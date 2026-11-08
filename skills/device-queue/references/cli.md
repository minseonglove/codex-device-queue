# Internal device queue execution reference

This reference is for agent execution and developer diagnostics. Normal users invoke the five skills; execute helpers on their behalf and return the observed result, never a Python command or a request for tickets/tokens. For status, cancellation and return, use [the management contract](management.md).

Bundled POSIX command: `python3 "$QUEUE_CLI"`. On Windows, use the absolute interpreter and PowerShell command supplied by startup context, including `-X utf8`. The shell examples below use POSIX syntax; adapt quoting and variables to the actual shell, and never pass Unix `shlex` quoting to PowerShell/cmd. Resolve `QUEUE_CLI` to the absolute `scripts/device_queue/cli.py` inside the installed plugin; the startup hook supplies the full command. No separate CLI installation is required.

Python 3.7+ standard-library implementation. No server, account, API key, package installation, shell startup changes, or recurring automation is required. All projects and worktrees using the same installed plugin share `.device-queue-state@shared/queue.sqlite3` in its version-common cache root. The installed CLI path selects that root independently of its version or the caller’s current directory. Source checkouts fall back to the configured CODEX_HOME cache. Different installations do not share queues. Leave `--state-dir` unset during real work; it exists for isolated tests.

## Automatic use by Codex

The plugin bundles a queue skill and SessionStart/SubagentStart hooks. Once installed, enabled and trusted, the hooks automatically inject mandatory queue instructions. Start a new Codex session after installation. Hooks also cover resume, clear, fork and compaction. Merely installing a skill does not guarantee that the full rules are loaded. This is agent instruction, not OS-level interception of all device tools.

## Recommended: enqueue, wait, execute, return

Prepare a script containing the entire continuous device test (installation, launch, interaction, result capture). Build locally first where practical. Then:

```sh
python3 "$QUEUE_CLI" run --owner "$QUEUE_OWNER" --device '<stable-hardware-id>' --purpose 'Verify the current change on the physical device' -- /absolute/path/to/device-test-script
```

`run` registers a ticket, waits in the local process until it obtains the exclusive grant, executes the command, and returns the grant. Nonzero command exits still return the grant when no device-dependent descendants remain. SIGINT/SIGTERM stop the registered process group before releasing. Unhandled termination retains the persistent ticket for inspection/recovery. Windows uses Job Objects instead of POSIX process groups: a killed controller stops its owned subprocess tree, but does not automatically close its durable request. Verify device-side cleanup before explicit recovery. Queue lifecycle events are JSON on stderr; command stdout/stderr are inherited. Exit status is the command's status on normal execution.

Set `QUEUE_OWNER` to the actual owner supplied by the startup hook and pass `--owner "$QUEUE_OWNER"` on every `request` or `run`, as shown below. A subagent must use the hook-supplied parent-session-plus-agent ID; it must not rely on an inherited parent `CODEX_THREAD_ID` or reuse the parent grant. For a root session only, omitting `--owner` defaults to `CODEX_THREAD_ID`. Outside Codex, supply an actual stable unique session ID. Do not use a project or branch name as the owner.

The command receives `DEVICE_QUEUE_DEVICE` and `DEVICE_QUEUE_TICKET`. The supplied device ID is opaque and case-sensitive. Discover the real hardware ID using the existing device-inventory tools, use the same exact ID across sessions and tools, and explicitly target that ID in the test commands. The queue does not select an OS, discover connected hardware, rewrite command arguments, or infer aliases between different IDs for the same hardware.

The default wait is indefinite and runs locally; it does not require repeated LLM inference. To bound the total queue wait, pass `--wait-timeout 60`; if it times out, that `run` cancels its own queued request and exits 3. While active, Codex can retain the tool process and wait for its result. A finished/inactive chat is not automatically awakened.

## Urgent work: insert at the front without interrupting the holder

Users can explicitly select the `device-queue` skill and append `--front` to its input. This is an explicit priority request; forward the option to a new internal `request` or `run`. It is not a separate skill or a terminal command for the user to execute.

The agent chooses `--front` automatically for a new job when the user asks for priority in any language, or when concrete task evidence establishes urgency, such as a confirmed active incident or an at-risk release deadline. The user need not mention the flag, and no separate approval is required. The agent briefly explains the reason and records it in `--purpose`.

An explicit instruction to keep normal order takes precedence. Otherwise default to FIFO when urgency is unclear. A short test, ordinary waiting, or a session being blocked does not by itself justify priority. Step ordering such as "install first, then launch" is not a request to jump the device queue.

For example, when the user requests urgent verification:

```sh
python3 "$QUEUE_CLI" run --owner "$QUEUE_OWNER" --device '<stable-hardware-id>' --purpose 'Urgent verification requested by the user' --front -- /absolute/path/to/device-test-script
```

For an interactive test, add the same `--front` option to `request` instead. The new ticket goes before all pending tickets for that device. A granted ticket always remains first, including pauses between its device commands. If A holds the device and B is waiting, inserting urgent C yields A → C → B. This does not cancel, signal, reclaim, or interrupt A; C must still obtain an explicit grant after A releases.

Each `--front` is a literal prepend, so a newer front request precedes an older front request. Normal requests keep their relative FIFO order. Other devices' queues are unaffected. Repeated urgent arrivals can postpone ordinary requests, so do not make this an agent default. A retry of an existing owner/device request returns its existing ticket/token/position; `request --front` does not promote an already registered ticket. Never cancel another session to make room.

The queue-order schema upgrades transactionally while preserving existing tickets, tokens, grants, and ordinary order. Database guards append requests from older clients at the end and leave their out-of-turn grant attempts queued, so already-loaded FIFO-only waiters cannot skip urgent work or override an existing grant. Existing granted jobs can finish and return their grants normally. Read positions with the updated `status` command; older clients still format their own responses using the older FIFO logic.

## Interactive work spanning several tool calls

Use this for a test that needs screenshots or UI/MCP calls between commands:

```sh
python3 "$QUEUE_CLI" request --owner "$QUEUE_OWNER" --device '<stable-hardware-id>' --purpose 'Interactive device verification'
python3 "$QUEUE_CLI" wait --ticket '<returned-ticket>' --token '<returned-token>' --timeout 30
python3 "$QUEUE_CLI" exec --ticket '<returned-ticket>' --token '<returned-token>' -- /absolute/path/to/device-command '<arguments>'
python3 "$QUEUE_CLI" release --ticket '<returned-ticket>' --token '<returned-token>'
```

`request` returns a secret ownership token and a queued ticket, even if the device is currently idle. It never grants access. A retry by the same owner/device returns the existing active ticket/token without changing its queue position. Preserve these values within the owning session; `status` never exposes tokens. `wait` grants atomically only at the head of the device queue. Exit 3 means it remains queued: repeat `wait` with the same ticket, not `request` with a new identity. Closed tickets cannot be acquired again.

After the grant, keep it through all dependent steps, including UI/MCP operations, and return it in cleanup. Shell commands should go through `exec`. UI/MCP tools cannot inherit the local lock, so their access depends on sessions obeying the ownership rule; finish/cancel their ongoing device activity before release. This tool has no automatic lease expiry.

Cancel an abandoned pending request:

```sh
python3 "$QUEUE_CLI" cancel --ticket '<returned-ticket>' --token '<returned-token>'
```

Do not call `run` when this session already has an active request for that device. Resume it using `wait`/`exec`, then release it.

## Inspect and recover

```sh
python3 "$QUEUE_CLI" status
python3 "$QUEUE_CLI" status --device '<stable-hardware-id>'
python3 "$QUEUE_CLI" status --history
```

Every device has its own queue; ordinary requests append FIFO, with explicit `--front` insertion as described above. Different devices can execute concurrently. The persistent queue is not deleted on device disconnection. A dead session or old timestamp never automatically frees a grant. Pending manual requests remain queued until their owner cancels or a verified administrative recovery closes them.

Recovery is an explicit operation, never a timer-based takeover:

```sh
python3 "$QUEUE_CLI" recover --ticket '<abandoned-ticket>' --confirm-idle --reason 'Verified the former workload and device-side test activity have stopped'
```

Use `--confirm-idle` only after verifying that no external tool, debugger, UI action, or device-side test from the old owner can continue. Recovery rejects a live managed-run controller, an inherited lock held by managed work, or a registered live process group, even with the confirmation flag. It never kills another session's processes. Uncertain state stays blocked. A reused process/group ID can conservatively block recovery and requires inspection, not a forced override.

On Windows, a command leader that exits while descendants remain causes the Job Object to stop those descendants; an unsafe/busy managed run retains its grant for inspection. On POSIX, managed commands must remain attached to their process group; do not daemonize, call `setsid`, or deliberately close inherited locks to escape tracking. The tool checks surviving descendants in the original process group. It cannot prove that a detached service or device-side operation has stopped. Finish those within the test script or keep an interactive grant until you have verified cleanup. This is cooperative coordination between Codex sessions, not a security boundary against the same OS user deliberately bypassing it.

## Exit statuses and tests

Control operations output JSON. Exit 0 is success, 2 is invalid/unavailable/unauthorized or a closed request, 3 is a queue-wait timeout, and 4 means active work prevents the operation. `run`/`exec` return the child command status when the command finishes normally (so inspect context if a command itself returns 2/3/4). Signal cancellation returns 128 plus the signal number. Inspect stderr and `status` after abnormal termination; do not assume a nonzero exit freed the device.

Run the installed subprocess tests with isolated temporary queues and harmless local scripts:

```sh
python3 -m unittest discover -s tests -v
```

The tests do not access a physical device. They verify coordination and local process lifecycle on the host where they are executed, not Android/iOS test-driver cleanup or device permissions. Python runtime references: [SQLite](https://docs.python.org/3.13/library/sqlite3.html), [subprocess](https://docs.python.org/3.13/library/subprocess.html), [file locks](https://docs.python.org/3.13/library/fcntl.html).

Native lifecycle tests require a Codex executable with plugin support; set `DEVICE_QUEUE_TEST_CODEX` to its absolute path if the default executable is older. Native Windows and WSL use different home/state roots; do not mix them for a shared device.

## Native plugin removal and storage migration

Finish all device work, cancel pending requests and return grants across participating sessions before standard `codex plugin remove codex-device-queue@codex-device-queue`. Native removal deletes the entire plugin cache root, including shared queue history and locks. No removal skill, `--uninstall` option or watcher exists. Updating preserves shared state because its directory name contains `@` and is not a valid plugin version.

Legacy home-wide state is migrated only when no queued/granted request, tracked live group or held device lock remains. The helper fences legacy writes, backs up closed history, then moves the legacy directory inside the same cache removal boundary. Across filesystems it deletes the verified legacy copy instead; an atomic cache receipt supports retrying partial cleanup. On `device_busy`, finish old work/waits and retry. On `storage_conflict` or unexpected files, stop and report the concrete conflict; never merge/delete state manually. Restart old sessions after updating; standalone legacy clients are no longer compatible. Native removal itself cannot be intercepted to check active work, so the user must finish that work before removing the plugin.
