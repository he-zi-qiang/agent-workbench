@echo off
rem  Run the computer-use MCP server on this Windows machine.
rem
rem  It is the one piece of this deployment that no container can hold: a
rem  screen adapter needs the desktop it acts on, and the desktop is here,
rem  not in Docker Desktop's VM. So it runs beside the containers, on the
rem  host, and the API inside its container reaches its read-only /session
rem  route through a loopback tunnel to host.docker.internal:8768 (ADR-0108).
rem
rem  This is the only part of the Windows route that needs a Python, and it
rem  asks for it through uv: uv fetches a 3.12 of its own if the machine has
rem  none, so the one thing to install by hand is uv itself.
rem
rem      winget install --id astral-sh.uv -e
rem
rem  Then, from a terminal (or by double-clicking this file):
rem
rem      scripts\computer.cmd            sync the computer-use extra, start the server
rem      scripts\computer.cmd --port N   ...on a different port (the API expects 8768)
rem
rem  The two conventions scripts\stack.cmd keeps, for the same reasons: ASCII
rem  only, because cmd.exe reads this in the console OEM code page; and no
rem  redirection, pipe or conditional character inside a rem line, because
rem  cmd splits a rem line on one and runs what follows.
rem
rem  What it does NOT do is grant anything. Every application the model may
rem  touch is approved by a person in a dialog this server puts up, per
rem  session, and the list starts empty every time it starts (ADR-070,
rem  ADR-076). Windows has no permission to grant this process beforehand:
rem  the one thing that blocks it is an elevated window in front, whose input
rem  Windows refuses on behalf of the person, and the server reports that
rem  rather than pretending the click landed.

setlocal

set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

pushd "%~dp0.."
if errorlevel 1 (
    echo computer: cannot enter the repository root 1>&2
    set "RC=1"
    goto :finish
)

uv --version >nul 2>nul
if errorlevel 1 (
    echo computer: no uv on PATH. 1>&2
    echo          Install it with:  winget install --id astral-sh.uv -e 1>&2
    echo          then reopen the terminal so PATH is picked up, and run this again. 1>&2
    set "RC=1"
    goto :popped
)

rem  --frozen: the lock file is the contract, here as in CI. --extra
rem  computer-use is the Windows half of that extra (Pillow); the pyobjc
rem  entries carry a darwin marker and are skipped on this platform. The sync
rem  is idempotent and fast once the environment exists.
echo Preparing the environment (uv sync --frozen --extra computer-use)...
uv sync --frozen --extra computer-use
if errorlevel 1 (
    echo computer: uv sync failed -- see the output above. 1>&2
    set "RC=1"
    goto :popped
)

echo.
echo Starting the computer-use MCP server on 127.0.0.1:8768.
echo   The console's Computer page reads its session through the API.
echo   An MCP client on this machine reaches its tools at
echo   http://127.0.0.1:8768/mcp -- nothing outside this machine can.
echo   Every application is approved in a dialog, per session. Ctrl+C stops it.
echo.
uv run --no-sync agent-computer-mcp %*
set "RC=%errorlevel%"
goto :popped

:popped
popd

:finish
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
