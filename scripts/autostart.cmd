@echo off
rem  Bring the stack back by itself after this machine reboots (ADR-0111).
rem
rem      scripts\autostart.cmd install   start the stack at every logon
rem      scripts\autostart.cmd remove    stop doing that
rem      scripts\autostart.cmd status    is it installed, and what did it last do
rem      scripts\autostart.cmd run       do now what the logon entry does
rem
rem  What a reboot leaves behind is nine long-running containers sitting in
rem  `Exited (255)` -- the code a container gets when the engine was stopped
rem  under it -- and nothing that brings them back.
rem
rem  THE OBVIOUS FIX IS THE WRONG ONE HERE, and ADR-0111 carries the argument.
rem  `restart: unless-stopped` lets the daemon start each container on its own,
rem  and `depends_on` does not apply to a daemon restart. Three things in this
rem  stack are decided once at startup and never revisited: whether the sandbox
rem  is on (`docker/decide_sandbox.py`), whether web search is on
rem  (`docker/decide_web_search.py`), and the Worker's MCP tool catalogue,
rem  which is frozen at start and fail-soft on discovery failure. Start those
rem  processes before the broker and the encoder and you get a stack that is
rem  entirely green and quietly missing features -- worse than one that is
rem  plainly down, because nothing says so.
rem
rem  So this replays the ORDERED start instead. `compose up -d --wait` honours
rem  `depends_on`, waits on `service_healthy` for the encoder that needs
rem  minutes to load three models, and waits on `service_completed_successfully`
rem  for migrate, qdrant-ready and provider-key-init.
rem
rem  It does NOT build. An image build fired by a logon is far worse than a
rem  stack that did not come up: tens of minutes, unasked, on every boot that
rem  missed a layer. A missing image makes `up` fail and say so in the log, and
rem  a person runs scripts\stack.cmd.
rem
rem  It does NOT open a browser. scripts\stack.cmd does, because somebody typed
rem  it and is looking at the window. Nobody is looking at a logon.
rem
rem  The two conventions scripts\stack.cmd keeps, for the same reasons: ASCII
rem  only, because cmd.exe reads this in the console OEM code page; and no
rem  redirection, pipe or conditional character inside a rem line, because cmd
rem  splits a rem line on one and runs what follows.

setlocal

set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

pushd "%~dp0.."
if errorlevel 1 (
    echo autostart: cannot enter the repository root 1>&2
    set "RC=1"
    goto :finish
)

set "LOGFILE=%CD%\var\autostart.log"

rem  The Startup folder, not a scheduled task.
rem
rem  Both `Register-ScheduledTask` and `schtasks /create` answer "Access is
rem  denied" for an ordinary logged-in user here (measured 2026-09-11), so a
rem  launcher built on one would work only from an elevated terminal -- and
rem  asking somebody to run a project launcher as Administrator to get their
rem  containers back is a worse trade than the thing it buys. The Startup
rem  folder needs no elevation, a person can see what is in it without a tool,
rem  and removing it is deleting a file.
set "STARTUPDIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "STUB=%STARTUPDIR%\AgentWorkbench-Stack.cmd"

if /i "%~1"=="install" goto :install
if /i "%~1"=="remove"  goto :remove
if /i "%~1"=="status"  goto :status
if /i "%~1"=="run"     goto :run

echo Usage: 1>&2
echo   scripts\autostart.cmd install   start the stack at every logon 1>&2
echo   scripts\autostart.cmd remove    stop doing that 1>&2
echo   scripts\autostart.cmd status    is it installed, and what did it last do 1>&2
echo   scripts\autostart.cmd run       do now what the logon entry does 1>&2
set "RC=1"
goto :popped

:install
if not exist "%STARTUPDIR%" (
    echo autostart: no Startup folder at 1>&2
    echo            %STARTUPDIR% 1>&2
    set "RC=1"
    goto :popped
)

rem  A stub that hands off and exits, rather than the work inline. The Startup
rem  folder runs this with a console window attached and the work takes about a
rem  minute; a window sitting in front of somebody's fresh desktop for a minute
rem  is the kind of thing that gets deleted without being read. `start /min`
rem  puts the work behind, and the stub is gone at once.
rem
rem  `start` takes the program and its arguments separately, so the checkout
rem  path is quoted once and needs no escaping even with a space in it. The
rem  empty "" is the window title `start` would otherwise mistake the quoted
rem  path for.
> "%STUB%" echo @echo off
>>"%STUB%" echo rem  Written by scripts\autostart.cmd install (ADR-0111).
>>"%STUB%" echo rem  To stop the stack starting at logon, delete this file or run:
>>"%STUB%" echo rem      scripts\autostart.cmd remove
>>"%STUB%" echo start "" /min "%CD%\scripts\autostart.cmd" run
if not exist "%STUB%" (
    echo autostart: could not write the startup entry at 1>&2
    echo            %STUB% 1>&2
    set "RC=1"
    goto :popped
)
echo Installed as a Startup entry:
echo   %STUB%
echo.
echo At every logon it waits for the Docker engine, then starts the stack in a
echo minimised window, writing what happened to:
echo   %LOGFILE%
echo.
echo Docker Desktop has to be running for that to work, and it does not start
echo itself unless its own setting says to -- Settings, General, "Start Docker
echo Desktop when you sign in". This entry waits for the engine either way, so
echo the two do not need to be ordered.
echo.
echo To undo:  scripts\autostart.cmd remove
set "RC=0"
goto :popped

:remove
if not exist "%STUB%" (
    echo autostart: there is no startup entry, so there is nothing to remove. 1>&2
    set "RC=1"
    goto :popped
)
del "%STUB%" >nul 2>nul
if exist "%STUB%" (
    echo autostart: could not delete 1>&2
    echo            %STUB% 1>&2
    set "RC=1"
    goto :popped
)
echo Removed. The stack no longer starts at logon. Whatever is running right
echo now is untouched.
set "RC=0"
goto :popped

:status
if not exist "%STUB%" (
    echo autostart: not installed. To install: scripts\autostart.cmd install
    set "RC=0"
    goto :popped
)
echo Installed as a Startup entry:
echo   %STUB%
echo.
if not exist "%LOGFILE%" (
    echo It has not run yet -- there is no %LOGFILE%.
    set "RC=0"
    goto :popped
)
echo The last thing it wrote:
echo.
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOGFILE%' -Tail 12"
set "RC=0"
goto :popped

:run
if not exist "var" mkdir "var"

call :log "autostart: waiting for the Docker engine"

rem  Twenty minutes of probing, once every ten seconds. Long, because what is
rem  being waited on is Docker Desktop starting a WSL 2 virtual machine on a
rem  machine that just booted and is doing everything else at the same time.
rem  Bounded, because a wait with no end cannot be told apart from a wait that
rem  is working.
set "WAITED=0"
:wait_engine
docker info >nul 2>nul
if not errorlevel 1 goto :engine_up
set /a WAITED+=10
if %WAITED% GEQ 1200 (
    call :log "autostart: the Docker engine did not answer within 1200s. Start Docker Desktop, then run scripts\stack.cmd"
    set "RC=1"
    goto :popped
)
powershell -NoProfile -Command "Start-Sleep -Seconds 10" >nul 2>nul
goto :wait_engine

:engine_up
call :log "autostart: engine answered after %WAITED%s, starting the stack"

rem  The same command scripts\stack.cmd runs, and deliberately the same one: a
rem  second place that knows how this stack comes up is a second answer, and
rem  the two will eventually disagree.
docker compose --profile demo up -d --wait --wait-timeout 600 >>"%LOGFILE%" 2>&1
if errorlevel 1 (
    call :log "autostart: the stack did not come up healthy. Try: scripts\stack.cmd logs"
    set "RC=1"
    goto :popped
)
call :log "autostart: the stack is up. Console: http://127.0.0.1:8000/ui/"
set "RC=0"
goto :popped

:log
rem  The timestamp comes from PowerShell rather than %DATE%, which is
rem  localized: on a Chinese Windows it carries a weekday in Chinese, and this
rem  file is ASCII-only precisely because cmd writes in the console OEM code
rem  page -- so the log ended up with mojibake in it. An ISO timestamp is the
rem  same everywhere and sorts.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "Get-Date -Format s"`) do set "TS=%%t"
echo [%TS%] %~1>>"%LOGFILE%"
echo [%TS%] %~1
goto :eof

:popped
popd

:finish
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
