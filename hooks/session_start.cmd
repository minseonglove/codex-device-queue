@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -3 -c "import sys; sys.exit(sys.version_info < (3,7))" >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0session_start.py"
  exit /b
)
python3 -c "import sys; sys.exit(sys.version_info < (3,7))" >nul 2>&1
if not errorlevel 1 (
  python3 "%~dp0session_start.py"
  exit /b
)
python -c "import sys; sys.exit(sys.version_info < (3,7))" >nul 2>&1
if not errorlevel 1 (
  python "%~dp0session_start.py"
  exit /b
)
echo device-queue: Python 3.7 or later was not found. Install Python and restart Codex. 1>&2
exit /b 2
