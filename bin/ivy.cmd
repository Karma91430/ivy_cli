@echo off
REM ivy.cmd — Windows launcher for the IVY CLI assistant.
REM Mirrors bin/ivy (bash) — runs from any working dir, inherits %CD% as
REM the project root for filesystem tools.

setlocal

REM Resolve the directory this script lives in (works even when on PATH).
set "SCRIPT_DIR=%~dp0"
set "CLI_DIR=%SCRIPT_DIR%.."

set "VENV_PY=%CLI_DIR%\.venv\Scripts\python.exe"
set "ENTRY=%CLI_DIR%\mcp_client.py"

if not exist "%VENV_PY%" (
    echo ivy: virtualenv not found at %CLI_DIR%\.venv 1>&2
    echo Set it up once with: 1>&2
    echo     py -3.12 -m venv "%CLI_DIR%\.venv" 1>&2
    echo     "%CLI_DIR%\.venv\Scripts\pip" install -r "%CLI_DIR%\requirements.txt" 1>&2
    exit /b 1
)

REM Run from the caller's %CD% so IVY inherits the working directory.
"%VENV_PY%" "%ENTRY%" %*
