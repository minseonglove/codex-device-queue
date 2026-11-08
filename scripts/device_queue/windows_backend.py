"""Win32 locks and owned process trees. Imported lazily by the Windows runner."""

import ctypes as C
import os
import subprocess
import sys
import time

# Win32 DWORD/LONG stay 32-bit even on hosts where C long is 64-bit.
DWORD, BOOL, HANDLE = C.c_uint32, C.c_int32, C.c_void_p
SIZE_T, LONGLONG = C.c_size_t, C.c_int64


class OVERLAPPED(C.Structure):
    _fields_ = [('Internal', SIZE_T), ('InternalHigh', SIZE_T),
                ('Offset', DWORD), ('OffsetHigh', DWORD), ('hEvent', HANDLE)]


class BASIC_LIMIT(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', LONGLONG), ('PerJobUserTimeLimit', LONGLONG),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T),
                ('MaximumWorkingSetSize', SIZE_T), ('ActiveProcessLimit', DWORD),
                ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class ACCOUNTING(C.Structure):
    _fields_ = [(name, LONGLONG) for name in (
        'TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')]
    _fields_ += [(name, DWORD) for name in (
        'TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


_kernel = None


def kernel():
    global _kernel
    if _kernel is None:
        dll = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CloseHandle': ([HANDLE], BOOL),
            'LockFileEx': ([HANDLE, DWORD, DWORD, DWORD, DWORD, C.POINTER(OVERLAPPED)], BOOL),
            'OpenProcess': ([DWORD, BOOL, DWORD], HANDLE),
            'WaitForSingleObject': ([HANDLE, DWORD], DWORD),
            'CreateJobObjectW': ([C.c_void_p, C.c_wchar_p], HANDLE),
            'OpenJobObjectW': ([DWORD, BOOL, C.c_wchar_p], HANDLE),
            'SetInformationJobObject': ([HANDLE, C.c_int, C.c_void_p, DWORD], BOOL),
            'QueryInformationJobObject': ([HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p], BOOL),
            'AssignProcessToJobObject': ([HANDLE, HANDLE], BOOL),
            'TerminateJobObject': ([HANDLE, C.c_uint32], BOOL),
            'MoveFileExW': ([C.c_wchar_p, C.c_wchar_p, DWORD], BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
        _kernel = dll
    return _kernel


def checked(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


def lock_file(fd, blocking):
    import msvcrt
    overlap = OVERLAPPED()
    # A synchronous CRT handle blocks in LockFileEx; one byte may extend past EOF.
    if not kernel().LockFileEx(msvcrt.get_osfhandle(fd), 2 | (0 if blocking else 1),
                              0, 1, 0, C.byref(overlap)):
        error = C.get_last_error()
        if error == 33:
            raise BlockingIOError('The file is already locked')
        raise C.WinError(error)


def process_alive(pid):
    if not pid:
        return False
    handle = kernel().OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = C.get_last_error()
        if error == 87:  # PID no longer exists.
            return False
        if error == 5:  # Access denied is not evidence of exit.
            return True
        raise C.WinError(error)
    try:
        result = kernel().WaitForSingleObject(handle, 0)
        if result == 258:
            return True
        if result == 0:
            return False
        raise C.WinError(C.get_last_error())
    finally:
        kernel().CloseHandle(handle)


def job_name(pid):
    # The database is shared across this user's Windows login sessions.
    return 'Global\\CodexDeviceQueue-%d' % pid


def active_processes(handle):
    accounting = ACCOUNTING()
    checked(kernel().QueryInformationJobObject(handle, 1, C.byref(accounting),
                                               C.sizeof(accounting), None))
    return accounting.ActiveProcesses


def group_alive(pid):
    handle = kernel().OpenJobObjectW(4, False, job_name(pid))  # JOB_OBJECT_QUERY
    if not handle:
        error = C.get_last_error()
        if error == 2:
            # Missing tracking with a live root must conservatively block recovery.
            return process_alive(pid)
        raise C.WinError(error)
    try:
        return bool(active_processes(handle))
    finally:
        kernel().CloseHandle(handle)


class Job:
    def __init__(self, pid):
        self.handle = None
        api = kernel()
        C.set_last_error(0)
        handle = checked(api.CreateJobObjectW(None, job_name(pid)))
        if C.get_last_error() == 183:
            kernel().CloseHandle(handle)
            raise OSError('A process job with this ID already exists; refusing to reuse it')
        self.handle = handle
        try:
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            checked(kernel().SetInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits)))
            process = checked(kernel().OpenProcess(0x0101, False, pid))
            try:
                checked(kernel().AssignProcessToJobObject(handle, process))
            finally:
                kernel().CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def stop(self):
        checked(kernel().TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 3
        while active_processes(self.handle):
            if time.monotonic() >= deadline:
                raise OSError('Owned Windows job has not stopped; grant must remain held')
            time.sleep(.03)

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            checked(kernel().CloseHandle(handle))


def spawn_worker(runner_path, gate_fd, command, env):
    import msvcrt
    handle = msvcrt.get_osfhandle(gate_fd)
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {'handle_list': [handle]}
    os.set_handle_inheritable(handle, True)
    try:
        return subprocess.Popen([sys.executable, '-X', 'utf8', runner_path,
                                 '--windows-worker', str(handle), '--'] + command,
                                startupinfo=startup, close_fds=True, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    finally:
        os.set_handle_inheritable(handle, False)


def gate_descriptor(handle):
    import msvcrt
    return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)


def publish_file(source, destination):
    # REPLACE_EXISTING | WRITE_THROUGH: publish before deleting the legacy copy.
    checked(kernel().MoveFileExW(str(source), str(destination), 0x9))
