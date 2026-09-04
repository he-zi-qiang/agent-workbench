@echo off
rem  Bring the whole stack up on Windows and open the console.
rem
rem  This is the Windows counterpart of `scripts/dev.sh` for the one job dev.sh
rem  cannot do here: dev.sh is bash, and it drives native processes that want
rem  uv, a Python 3.12 and a Node 24. This drives Compose instead, so the only
rem  thing the machine needs is Docker Desktop.
rem
rem      scripts\stack.cmd            build, start, wait for healthy, open the console
rem      scripts\stack.cmd down       stop everything and remove the containers
rem      scripts\stack.cmd logs       follow the logs
rem      scripts\stack.cmd status     what is running
rem      scripts\stack.cmd restart    restart the API, both Workers and the sandbox
rem      scripts\stack.cmd sandbox-image   build the sandbox image that can draw a PDF
rem
rem  Computer use is not in this file: a container cannot reach the desktop,
rem  so that server runs on this Windows itself. See scripts\computer.cmd.
rem
rem  Two conventions this file keeps, inherited from scripts/panel.cmd and for
rem  the same reasons:
rem
rem  ASCII only. cmd.exe reads a batch file in the console OEM code page rather
rem  than UTF-8, so a Chinese comment in here arrives as mojibake on most
rem  installs.
rem
rem  No redirection, pipe or conditional characters inside a rem line. cmd
rem  splits a rem line on a conditional operator and runs what follows.

setlocal

set "HOLD="
echo(%cmdcmdline%| find /i "%~nx0" >nul 2>nul
if not errorlevel 1 set "HOLD=1"

rem  pushd rather than cd /d, so a checkout reached over a share still works.
pushd "%~dp0.."
if errorlevel 1 (
    echo stack: cannot enter the repository root 1>&2
    set "RC=1"
    goto :finish
)

rem  Docker missing and Docker installed-but-not-running are the two failures
rem  on Windows, they need different answers, and only the first is obvious
rem  from the error Docker prints. Probe each separately.
rem
rem  Probed by running rather than by asking whether the name resolves: a
rem  Docker Desktop that has been uninstalled commonly leaves docker.exe shims
rem  on PATH, which resolve and then fail.
docker --version >nul 2>nul
if errorlevel 1 (
    echo stack: no docker on PATH. 1>&2
    echo        Install Docker Desktop from docker.com, reopen the terminal so 1>&2
    echo        PATH is picked up, and run this again. 1>&2
    set "RC=1"
    goto :popped
)

docker info >nul 2>nul
if errorlevel 1 (
    echo stack: Docker is installed but the engine is not running. 1>&2
    echo        Start Docker Desktop, wait until its whale icon stops animating, 1>&2
    echo        then run this again. 1>&2
    set "RC=1"
    goto :popped
)

if /i "%~1"=="down"   goto :down
if /i "%~1"=="logs"   goto :logs
if /i "%~1"=="status" goto :status
if /i "%~1"=="restart" goto :restart
if /i "%~1"=="sandbox-image" goto :sandbox_image

rem  Memory, asked before the build rather than discovered during it, and
rem  only on the path that builds. `down`, `logs`, `status`, `restart` and
rem  `sandbox-image` have already branched away above.
rem
rem  ONE process in this stack loads the retrieval model set: the encoder
rem  service (ADR-0106). The API, both Task Workers and the ingestion worker
rem  ask it over HTTP and load no model at all. Before that ADR all four
rem  loaded a full set each, and the floors here were four times what they
rem  are now.
rem
rem  One measured number exists, and it is for ONE process that holds the
rem  models, on the native path: about 12 GB of available memory, of which
rem  about 6.7 GB is the three model files themselves (2026-07-31,
rem  docs/running-locally.md). The lean processes, PostgreSQL, Qdrant and the
rem  collector have NOT been measured; the 4 GB between the two lines below
rem  is an allowance for them, not a second measurement.
rem
rem      12 GB   the one measured figure. Under it the encoder itself pages,
rem              so the stack cannot work, it can only swap.
rem      16 GB   the same figure plus the allowance for everything else.
rem
rem  Under the hard floor this stops, and stopping is the kinder answer: the
rem  alternative is tens of minutes of build followed by `up --wait` timing
rem  out in swap, which reads as "this project does not run" rather than as
rem  "this machine was not given enough memory". Docker Desktop hands the WSL 2
rem  VM about half of physical RAM by default, so a 32 GB machine meets the
rem  second line without touching a setting; a 16 GB one is under the first.
rem
rem  Compared by slicing digits off the byte count, not by `set /a`: MemTotal
rem  is bytes, and cmd's arithmetic is 32-bit signed, so anything above about
rem  2.1 GB overflows. Dropping the last nine digits is an integer divide by
rem  1e9 that cannot overflow -- so the numbers here and below are decimal GB.
set "MEMGB="
for /f "tokens=*" %%m in ('docker info --format "{{.MemTotal}}" 2^>nul') do set "MEMBYTES=%%m"
if defined MEMBYTES set "MEMGB=%MEMBYTES:~0,-9%"
if not defined MEMGB set "MEMGB=0"
if "%MEMGB%"=="" set "MEMGB=0"

if %MEMGB% GEQ 16 goto :memory_ok
if %MEMGB% GEQ 12 goto :memory_tight

echo stack: Docker has about %MEMGB% GB of memory. The encoder service, the 1>&2
echo        one process here that loads the retrieval models, needs about 1>&2
echo        12 GB on its own, so this stack would come up and then page 1>&2
echo        instead of working. Stopping here rather than after the build. 1>&2
echo. 1>&2
echo        Docker Desktop, Settings, Resources, Memory. On Windows that 1>&2
echo        slider is bounded by what WSL 2 may take, set in .wslconfig in 1>&2
echo        your user folder. See docs/windows-quickstart.md. 1>&2
echo. 1>&2
echo        To build and start it anyway: scripts\stack.cmd anyway 1>&2
if /i not "%~1"=="anyway" (
    set "RC=1"
    goto :popped
)
echo        Proceeding because you asked. 1>&2
goto :memory_ok

:memory_tight
echo Docker has about %MEMGB% GB of memory. The encoder service needs about
echo 12 GB of it on its own -- the one measured figure -- and PostgreSQL,
echo Qdrant and the other processes have not been measured, so expect this to
echo be slow. Ingestion is usually what suffers first.
echo.

:memory_ok

rem  Build with `docker build`, then `compose up` WITHOUT --build. That split
rem  looks redundant and is not.
rem
rem  `docker compose build` -- and therefore `compose up --build` -- goes
rem  through buildx bake, which sets a gRPC header, x-docker-expose-session-
rem  sharedkey, derived from the build context directory's own name. A name
rem  with a non-ASCII character makes that header invalid and the build dies
rem  before a single layer runs, with a message that mentions neither the path
rem  nor the directory:
rem
rem      failed to dial gRPC: ... header key "x-docker-expose-session-
rem      sharedkey" contains value with non-printable ASCII characters
rem
rem  Measured 2026-09-01, Docker 29.4.0, and it needs BOTH halves: two or more
rem  services sharing one build context (this file has eight today, every
rem  one of them building `context: .` into agent-workbench:local; it had
rem  four when this was measured) AND a non-ASCII directory name. One service with a
rem  non-ASCII name builds; four services under an ASCII name build; four under
rem  a non-ASCII name never do. COMPOSE_BAKE=false does not avoid it.
rem
rem  Plain `docker build` does not take that path and is unaffected -- verified
rem  against the same directory. A Windows checkout under a path like
rem  D:\projects\... is fine either way, and one under D:\Chinese-name\... is
rem  the common case here, so the two-step is unconditional rather than
rem  conditional on a check that would have to guess at the same rule.
echo Building the image. First run pulls Node 24, Python 3.12, the Docker
echo CLI and the retrieval runtime, then downloads about 6.7 GB of model
echo weights into a named volume. Expect tens of minutes, once.
docker build -t agent-workbench:local .
if errorlevel 1 (
    echo stack: image build failed -- see the output above. 1>&2
    rem  Stopping here matters. `compose up` would fall back to whatever
    rem  agent-workbench:local already exists and silently run a stale build.
    set "RC=1"
    goto :popped
)

rem  --profile demo, not the default stack. The default one is a control plane
rem  with no Task Worker in it, so a person who opens the console can look at
rem  Chat and at an empty task list and see nothing of claim, lease, epoch or
rem  fencing -- the part of this system that took the most work. The profile
rem  adds two Workers precisely because those invariants only mean anything
rem  under contention, plus an ingestion worker.
rem
rem  Both Workers run with --demo, which supplies build_demo_handlers(). That
rem  is what makes the v1 research graph buildable without the embedding extra,
rem  so the shipped default graph_version needs no override here. Verified
rem  2026-09-01: a task submitted with no graph named reached `succeeded`.
echo Starting the stack and waiting until every container reports healthy.
docker compose --profile demo up -d --wait --wait-timeout 600
if errorlevel 1 (
    echo stack: the stack did not come up healthy. Try: scripts\stack.cmd logs 1>&2
    set "RC=1"
    goto :popped
)

echo.
echo   Console  http://127.0.0.1:8000/ui/
echo   Stop     scripts\stack.cmd down
echo.
rem  Said at the one moment somebody is looking at this window.
rem
rem  This list has been getting shorter. Before the image carried the
rem  embedding extra and the Workers started their own MCP servers, the honest
rem  sentence here was "no embedding runtime and no MCP servers". ADR-0107
rem  took the sandbox off it, and ADR-0108 moved computer use from "absent"
rem  to "a second launcher on this machine". What is left is the one thing
rem  nobody has typed yet, and it is the one that looks like neither a bug
rem  nor an absence: a synthetic Worker takes a Task all the way to succeeded
rem  without ever calling a model or a tool.
rem
rem  Kept as a pointer rather than a full account. The System page names every
rem  absence and its remedy (ADR-102); this window only has to stop somebody
rem  from reading silence as completeness.
echo   Not everything is on. Without a provider key the Task Workers run
echo   SYNTHETIC handlers: tasks reach succeeded with no model call and no tool
echo   call. Save a key on the System page, flip what you want, then:
echo   scripts\stack.cmd restart
echo.
echo   Sandbox execution runs in the sandbox container, which alone holds the
echo   Docker socket; if its image could not be pulled the System page says
echo   the sandbox is absent, and scripts\stack.cmd logs says why. Computer
echo   use cannot run in a container at all: to have it, run
echo   scripts\computer.cmd on this machine (it needs uv, nothing else).
echo.
start "" "http://127.0.0.1:8000/ui/"
set "RC=0"
goto :popped

:down
docker compose --profile demo down
set "RC=%errorlevel%"
goto :popped

:logs
docker compose --profile demo logs -f
set "RC=%errorlevel%"
goto :popped

:status
docker compose --profile demo ps
set "RC=%errorlevel%"
goto :popped

:restart
rem  A key saved on the settings page, or a switch flipped on the System page,
rem  is read at the next start of the processes that read configuration once:
rem  the API and the Workers (ADR-101, ADR-103). The sandbox broker picks its
rem  image at start too, so a PDF image built by `sandbox-image` is only seen
rem  after this (ADR-0107). Restart exactly those four. PostgreSQL, Qdrant,
rem  the collector and the encoder keep running -- the encoder in particular,
rem  because restarting it means reloading three models -- so this takes
rem  seconds, where `down` and a fresh start would take the image build again.
docker compose --profile demo restart sandbox api task-worker task-worker-b
set "RC=%errorlevel%"
goto :popped

:sandbox_image
rem  The image that lets `sandbox_run` draw a PDF instead of only text
rem  (docker/sandbox-pdf.Dockerfile: a digest-pinned python:3.12-slim plus
rem  reportlab and a CJK TrueType font). The broker uses it when it exists
rem  and says on its log which of the two images it got; it never falls back
rem  silently. Plain `docker build` here for the reason the main build uses
rem  it: the bake path dies on a non-ASCII directory name.
docker build -t agent-workbench-sandbox-pdf:local -f docker\sandbox-pdf.Dockerfile docker
if errorlevel 1 (
    echo stack: the sandbox image did not build -- see the output above. 1>&2
    set "RC=1"
    goto :popped
)
echo Built agent-workbench-sandbox-pdf:local. The broker reads it at its next
echo start: scripts\stack.cmd restart
set "RC=0"
goto :popped

:popped
popd

:finish
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
