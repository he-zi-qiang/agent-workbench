@echo off
rem  Put an icon on the desktop that starts the whole stack.
rem
rem  Run it once, from Explorer or from a terminal. It creates a shortcut on the
rem  desktop -- named "Agent " plus U+5DE5 U+4F5C U+53F0, the product's own name,
rem  spelled in code points here for the reason below -- pointing at
rem  scripts\stack.cmd and wearing scripts\agent-workbench.ico.
rem
rem      scripts\shortcut.cmd          create it, or refresh one in place
rem      scripts\shortcut.cmd remove   delete it again
rem
rem  Why a shortcut rather than an icon on the .cmd itself: Windows takes a
rem  file's icon from its *type*, so every .cmd on a machine wears cmd.exe's
rem  icon and there is no per-file override. A .lnk carries its own
rem  IconLocation, and it is also the only one of the two that can be pinned to
rem  the taskbar or the Start menu.
rem
rem  Why this file generates the .lnk rather than the repository shipping one: a
rem  .lnk stores an absolute path, and this checkout sits at a different path on
rem  every machine. A shipped one would also arrive inside a downloaded ZIP,
rem  which means arriving with the mark of the web on it, which means Windows
rem  warning about a file the person just unpacked themselves.
rem
rem  The two conventions from scripts\stack.cmd and scripts\panel.cmd hold here
rem  for the same reasons: ASCII only, because cmd.exe reads a batch file in the
rem  console OEM code page rather than UTF-8, so a Chinese character in this file
rem  arrives as mojibake on most installs; and no redirection, pipe or
rem  conditional character inside a rem line, because cmd splits a rem line on a
rem  conditional operator and runs what follows.

setlocal

rem  Double-clicked? Explorer starts a .cmd through cmd /c, which puts this
rem  file's name into cmdcmdline. Unlike the other launchers this one holds the
rem  window open on SUCCESS as well, and that is the whole point: the only
rem  output this script has is the sentence saying where the icon went, and a
rem  window that closes on its own takes that sentence with it.
set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

rem  No pushd here, unlike every other launcher in this directory. They need the
rem  repository root as a working directory because they run things relative to
rem  it. This one runs nothing relative to anything, and pushd would actively
rem  hurt: for a checkout reached over a share it maps a temporary drive letter,
rem  and a shortcut holding that letter would point at a drive that stops
rem  existing the moment this window closes. %~dp0 is always fully qualified.
for %%i in ("%~dp0..") do set "AW_LNK_ROOT=%%~fi"
set "AW_LNK_TARGET=%~dp0stack.cmd"
set "AW_LNK_ICON=%~dp0agent-workbench.ico"

set "RC=1"
if not exist "%AW_LNK_TARGET%" (
    echo shortcut: stack.cmd is not next to this file -- is it still inside 1>&2
    echo           the checkout? 1>&2
    goto :finish
)
if not exist "%AW_LNK_ICON%" (
    echo shortcut: agent-workbench.ico is not next to this file. It is 1>&2
    echo           committed; if it has gone missing, python 1>&2
    echo           scripts\make_icon.py draws it again. 1>&2
    goto :finish
)

rem  Probed by running rather than by asking whether the name resolves, the same
rem  way stack.cmd probes Docker. powershell.exe ships with every Windows that
rem  can run Docker Desktop, so this failing means something unusual about the
rem  machine, and the message says what to do by hand instead of blaming it.
powershell -NoProfile -Command "exit 0" >nul 2>nul
if errorlevel 1 (
    echo shortcut: powershell.exe did not run. A .lnk is a COM object, and 1>&2
    echo           this is the only way to make one without adding a build 1>&2
    echo           step to a machine that was promised it needs only Docker. 1>&2
    echo           By hand instead: right-drag scripts\stack.cmd onto the 1>&2
    echo           desktop, choose Create shortcuts here, then Properties, 1>&2
    echo           Change Icon, and point it at scripts\agent-workbench.ico. 1>&2
    goto :finish
)

rem  The weights mirror, carried into the shortcut itself (see section 3 of
rem  docs/windows-quickstart.md). The first start downloads about 6.7 GB of
rem  model weights, and on a mainland-China connection that finishes only
rem  through a mirror -- but a double-clicked icon inherits no terminal, so
rem  HF_ENDPOINT set in a shell has never been able to reach it. Set it in the
rem  terminal where you run THIS script, and it is baked into the shortcut's own
rem  command line, where every later double-click finds it.
rem
rem  Read, never defaulted: which endpoint a deployment's weights come from is a
rem  supply-chain decision belonging to whoever runs the stack, which is the same
rem  reason docker/fetch_weights.py does not pick one either.
set "AW_LNK_MIRROR=%HF_ENDPOINT%"

set "AW_LNK_REMOVE="
if /i "%~1"=="remove" set "AW_LNK_REMOVE=1"

rem  Everything below is one PowerShell program, assembled in fragments so that
rem  each line stays readable. Two rules govern how it is spelled, and both come
rem  from this text being parsed twice -- first by cmd.exe, which counts quote
rem  characters and splits on conditional operators, and then by PowerShell:
rem
rem    * Not one literal quote or ampersand appears in it. Where the program
rem      needs them it writes [char]34 and [char]38. A quote here would flip
rem      cmd's idea of whether the rest of the line is quoted, and an ampersand
rem      that landed outside the quotes would end the command and run whatever
rem      followed it.
rem    * Every path travels through the environment, never on the command line.
rem      Then no escaping rule applies to any of them: a checkout under a name
rem      containing a space, a quote or an ampersand is data to getenv rather
rem      than syntax to anybody.
set "PS=$ErrorActionPreference='Stop';"
set "PS=%PS% $q=[char]34; $amp=[char]38;"
rem  GetFolderPath, not USERPROFILE\Desktop. With OneDrive's Known Folder Move
rem  on -- the default on a great many Windows 11 machines -- the real desktop
rem  lives under OneDrive and USERPROFILE\Desktop is a leftover folder nobody
rem  looks at. GetFolderPath asks the shell where it actually points.
set "PS=%PS% $desk=[Environment]::GetFolderPath('Desktop');"
set "PS=%PS% $name='Agent '+[char]0x5DE5+[char]0x4F5C+[char]0x53F0+'.lnk';"
set "PS=%PS% $path=Join-Path $desk $name;"
set "PS=%PS% if ($env:AW_LNK_REMOVE) {"
set "PS=%PS%   if (Test-Path -LiteralPath $path) {"
set "PS=%PS%     Remove-Item -LiteralPath $path; Write-Host ('removed ' + $path)"
set "PS=%PS%   } else { Write-Host ('nothing to remove at ' + $path) };"
set "PS=%PS%   exit 0 };"
set "PS=%PS% $w=New-Object -ComObject WScript.Shell;"
set "PS=%PS% $lnk=$w.CreateShortcut($path);"
rem  With a mirror the target becomes cmd.exe and the launcher moves into the
rem  arguments, behind a quoted `set` -- quoted so the value does not pick up the
rem  space that follows it. Without one the target is the launcher itself, which
rem  keeps the ordinary case a shortcut to a file rather than a shortcut to a
rem  command line.
rem
rem  Either way stack.cmd still finds its own name in cmdcmdline, so its
rem  double-click detection -- the thing that holds a failure on screen long
rem  enough to be read -- keeps working through the shortcut.
set "PS=%PS% if ($env:AW_LNK_MIRROR) {"
set "PS=%PS%   $lnk.TargetPath=$env:ComSpec;"
set "PS=%PS%   $lnk.Arguments='/c set '+$q+'HF_ENDPOINT='+$env:AW_LNK_MIRROR+$q+' '+$amp+$amp+' '+$q+$env:AW_LNK_TARGET+$q"
set "PS=%PS% } else { $lnk.TargetPath=$env:AW_LNK_TARGET };"
set "PS=%PS% $lnk.WorkingDirectory=$env:AW_LNK_ROOT;"
set "PS=%PS% $lnk.IconLocation=$env:AW_LNK_ICON+',0';"
set "PS=%PS% $lnk.Description='Build and start the agent-workbench stack, then open the console.';"
set "PS=%PS% $lnk.Save(); Write-Host ('created ' + $path);"
set "PS=%PS% if ($env:AW_LNK_MIRROR) { Write-Host ('weights mirror: ' + $env:AW_LNK_MIRROR) }"

powershell -NoProfile -Command "%PS%"
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo shortcut: PowerShell did not finish -- see the message above. 1>&2
    goto :finish
)
if defined AW_LNK_REMOVE goto :finish

rem  Said here rather than in the tooltip, because a tooltip is not where
rem  anybody reads anything. The Chinese name may come out as question marks in
rem  this window on an English-locale console -- that is the console's code
rem  page, not the shortcut, and the desktop draws the name correctly either way.
echo.
echo   Double-click it to build and start everything. The first run takes tens
echo   of minutes and wants about 16 GB given to Docker -- stack.cmd measures
echo   that before it spends the time, and says so when it is short.
echo.
echo   Pin it: right-click the icon, Pin to taskbar.
echo   Take it off the desktop again: scripts\shortcut.cmd remove
echo.

:finish
if defined HOLD pause
exit /b %RC%
