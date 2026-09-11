@echo off
rem  Clear the socket files that stop Docker Desktop from starting on Windows.
rem
rem  The failure this exists for looks like a broken Docker install and is not
rem  one. Docker Desktop puts up "An unexpected error occurred" and quits, and
rem  its log names the real cause:
rem
rem      backend cancelling with error: starting services: initializing Ingest
rem      server: listening on unix://.../Docker/run/sailor-ingest.sock: rename
rem      sailor-ingest.sock sailor-ingest.sock.stale: The file cannot be
rem      accessed by the system.
rem
rem  Those are AF_UNIX socket files. When the process that owned one is gone,
rem  Windows leaves the file behind in a state where it can be neither opened,
rem  deleted nor renamed -- and Docker Desktop's first act on startup is to
rem  rename it to .stale. So every subsequent start dies on the same line.
rem
rem  Measured 2026-09-11, Docker Desktop 4.89.0. THIS IS NOT CAUSED BY KILLING
rem  IT. The first time it was met it followed a forced stop and was written
rem  off as deserved; it then happened again after a clean `docker desktop
rem  stop` that returned 0. A normal stop leaves the sockets, and the next
rem  start trips on them. That is why this file exists rather than a line in a
rem  document saying "do not force-quit Docker".
rem
rem  Renaming the DIRECTORY works where deleting the files does not: the files
rem  cannot be touched, but their parent is an ordinary directory, and Docker
rem  Desktop recreates a missing one at startup. Both were tried; only this
rem  one works. The renamed directories are left behind rather than deleted,
rem  because deleting them fails for exactly the same reason -- they are
rem  empty of anything but the dead sockets, and a reboot is what clears them.
rem
rem  Two directories, both of which have been hit, in this order: Docker's own
rem  `run`, then the secrets engine's. Fixing only the first just moves the
rem  crash to the second, which is how the second one was found.
rem
rem  The two conventions scripts\stack.cmd keeps, for the same reasons: ASCII
rem  only, because cmd.exe reads this in the console OEM code page; and no
rem  redirection, pipe or conditional character inside a rem line, because cmd
rem  splits a rem line on one and runs what follows.
rem
rem      scripts\docker-unstick.cmd         clear them, then start Docker Desktop
rem      scripts\docker-unstick.cmd --no-start    clear them and stop there

setlocal

set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

rem  Refuse to run against a working Docker. This file force-stops Docker
rem  Desktop, which is the right thing to do to a process that has already
rem  crashed and the wrong thing to do to one that is serving containers.
rem  Probed by asking the engine, not by looking for the process: the crashed
rem  state leaves the GUI process alive with a dialog on it, so a process
rem  check would report health that is not there.
docker info >nul 2>nul
if not errorlevel 1 (
    echo docker-unstick: the Docker engine is answering, so there is nothing 1>&2
    echo                 stuck to clear. This file force-stops Docker Desktop 1>&2
    echo                 and would interrupt whatever is running. 1>&2
    set "RC=0"
    goto :finish
)

echo Docker is not answering. Clearing the socket directories it cannot start over.
echo.

taskkill /f /im "Docker Desktop.exe" >nul 2>nul
taskkill /f /im "com.docker.backend.exe" >nul 2>nul
taskkill /f /im "com.docker.build.exe" >nul 2>nul

rem  A stamp rather than a fixed name: this can happen more than once, and a
rem  second run must not fail because the first run's directory is in the way
rem  -- it cannot be deleted either.
set "STAMP=%RANDOM%"

set "MOVED=0"
call :retire "%LOCALAPPDATA%\Docker\run"
call :retire "%LOCALAPPDATA%\docker-secrets-engine"

echo.
if "%MOVED%"=="0" (
    echo Neither directory was there, so this was not the problem. Look at
    echo %LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log
    echo and search it for: backend cancelling with error
    set "RC=1"
    goto :finish
)

if /i "%~1"=="--no-start" (
    echo Not starting Docker Desktop, because you asked. Start it yourself.
    set "RC=0"
    goto :finish
)

echo Starting Docker Desktop. Give it a minute, then: docker info
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set "RC=0"
goto :finish

:retire
if not exist "%~1" (
    echo   %~nx1 is not there, nothing to do
    goto :eof
)
move "%~1" "%~1.orphan-%STAMP%" >nul 2>nul
if errorlevel 1 (
    echo   %~nx1 could not be moved -- something still holds it 1>&2
    goto :eof
)
echo   %~nx1 moved aside as %~nx1.orphan-%STAMP%
set "MOVED=1"
goto :eof

:finish
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
