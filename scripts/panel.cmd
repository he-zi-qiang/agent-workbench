@echo off
rem  Open the architecture panel on Windows.
rem
rem  This starts the same program that scripts/dev.sh panel starts. All this
rem  file adds is a way in from cmd.exe, from PowerShell, or from a double-click
rem  in Explorer -- none of which can run a bash script, and dev.sh is bash.
rem
rem  Two conventions this file keeps, both of which are load-bearing:
rem
rem  ASCII only. cmd.exe reads a batch file in the console OEM code page rather
rem  than UTF-8, so a Chinese comment in here arrives as mojibake on most
rem  installs. The panel prints Chinese; this launcher does not.
rem
rem  No redirection, pipe or conditional characters inside a rem line. Microsoft
rem  documents that a batch comment may not contain them, and the reason is
rem  visible in the documented multi-line-comment idiom: cmd splits a rem line
rem  on a conditional operator and runs what follows. A rem line quoting a shell
rem  snippet is therefore a command, and the parse error it raises is printed on
rem  every run, before anything useful happens. This file had one.
rem
rem  The panel imports nothing outside the standard library, so it needs no
rem  uv sync, no virtualenv, and none of the services the rest of dev.sh starts.
rem  Whatever Python the machine already has will do.
rem
rem      scripts\panel.cmd                  serve on 127.0.0.1:8770
rem      scripts\panel.cmd --port 9000      ...on a different port
rem      scripts\panel.cmd --no-open        do not open a browser
rem      scripts\panel.cmd --check          verify the hand-written half only

setlocal

rem  Was this double-clicked? Explorer starts a .cmd through cmd /c, which puts
rem  this file's name into cmdcmdline; a shell that runs it does not. The answer
rem  decides one thing only: whether a failure holds the window open long enough
rem  to be read. Getting it wrong in either direction is survivable, which is
rem  why it is allowed to be a heuristic.
set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

rem  pushd rather than cd /d. cmd.exe has never supported a UNC path as the
rem  current directory, so a checkout reached over a share -- the WSL case,
rem  \\wsl.localhost\..., is the realistic one -- would fail here and then fail
rem  again three lines down with a message saying the script is not inside the
rem  checkout, which it is. pushd maps a temporary drive letter for a UNC target
rem  and is the documented way around it.
pushd "%~dp0.."
if errorlevel 1 (
    echo panel: cannot enter the repository root 1>&2
    set "RC=1"
    goto :finish
)

set "PANEL=scripts\architecture_panel.py"
if not exist "%PANEL%" (
    echo panel: %PANEL% not found -- is this file still inside the checkout? 1>&2
    set "RC=1"
    goto :popped
)

rem  The script reconfigures its own stdout and stderr to UTF-8, but that only
rem  starts helping once it is running. This covers the window before that: an
rem  import error or a syntax error is written by the interpreter itself, and on
rem  an English-locale console that traceback would otherwise be unreadable.
set "PYTHONUTF8=1"

rem  Three candidates, and the order is the point rather than an alphabet.
rem
rem  py first: it is the launcher a python.org install puts on PATH, it is the
rem  one that reliably picks a 3.x on a machine with several, and it starts in
rem  milliseconds. python next, which is what the Microsoft Store install and
rem  most others leave behind.
rem
rem  uv last, and with --no-project. Everywhere else in this repository uv run
rem  is the right spelling, and here it is the wrong one: plain uv run syncs the
rem  project environment first, so on the fresh checkout where this command is
rem  most useful it would download the whole dependency tree in order to start a
rem  program that imports nothing outside the standard library. --no-project
rem  skips that, and uv still supplies an interpreter on a machine with none.
rem
rem  Each candidate is probed by running it, not by asking where the name
rem  resolves. Two traps make the difference:
rem
rem    Windows puts a python.exe on PATH even with no Python installed -- the
rem    Store app-execution alias. It resolves like any other PATH entry, opens a
rem    shop when executed, and exits 9009.
rem
rem    py.exe is an all-users component installed to C:\Windows and commonly
rem    outlives every Python it used to launch. It resolves, then exits 103
rem    because it found no runtime.
rem
rem  In both cases a name-resolution probe reports success and the fallback
rem  chain never runs.
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto :use_py
python -c "import sys" >nul 2>nul
if not errorlevel 1 goto :use_python
uv --version >nul 2>nul
if not errorlevel 1 goto :use_uv

echo panel: no py, python or uv on PATH. 1>&2
echo        Install Python from python.org or the Microsoft Store, reopen the 1>&2
echo        terminal so PATH is picked up, and run this again. 1>&2
set "RC=1"
goto :popped

:use_py
py -3 "%PANEL%" --serve %*
set "RC=%errorlevel%"
goto :popped

:use_python
python "%PANEL%" --serve %*
set "RC=%errorlevel%"
goto :popped

:use_uv
uv run --no-project python "%PANEL%" --serve %*
set "RC=%errorlevel%"
goto :popped

:popped
popd

:finish
rem  Hold a double-clicked window open on any failure, not just on the missing
rem  interpreter. The success path blocks on the server, so it never lands here
rem  while anything is still worth reading -- but a second launch does: the
rem  panel refuses to share its port on Windows, so it exits at once with an
rem  address-in-use error, and an Explorer window would close on top of it.
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
