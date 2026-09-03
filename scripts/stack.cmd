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
rem      scripts\stack.cmd restart    restart the API and both Workers, nothing else
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

rem  Memory, asked before the build rather than discovered during it, and
rem  only on the path that builds. `down`, `logs`, `status` and `restart` have
rem  already branched away above.
rem
rem  This stack runs four processes that each load the full retrieval model
rem  set: the API, both Task Workers and the ingestion worker. One measured
rem  number exists for that, and it is for ONE process on the native path --
rem  about 12 GB of available memory, of which about 6.7 GB is the three model
rem  files themselves (2026-07-31, docs/running-locally.md). The floors below
rem  are arithmetic on that number rather than a second measurement:
rem
rem      4 x 6.7 GB = about 29 GB   just to hold the weights. Under this the
rem                                 stack cannot work, it can only page.
rem      4 x 12  GB = about 51 GB   the per-process figure, four times.
rem
rem  Under the hard floor this stops, and stopping is the kinder answer: the
rem  alternative is tens of minutes of build followed by `up --wait` timing
rem  out in swap, which reads as "this project does not run" rather than as
rem  "this machine was not given enough memory". Docker Desktop hands the WSL 2
rem  VM about half of physical RAM by default, so this is usually a setting
rem  rather than a hardware limit.
rem
rem  Compared by slicing digits off the byte count, not by `set /a`: MemTotal
rem  is bytes, and cmd's arithmetic is 32-bit signed, so anything above about
rem  2.1 GB overflows. Dropping the last nine digits is an integer divide by
rem  1e9 that cannot overflow -- so the numbers here and below are decimal GB,
rem  which is why they read 29 and 51 rather than 27 and 48.
set "MEMGB="
for /f "tokens=*" %%m in ('docker info --format "{{.MemTotal}}" 2^>nul') do set "MEMBYTES=%%m"
if defined MEMBYTES set "MEMGB=%MEMBYTES:~0,-9%"
if not defined MEMGB set "MEMGB=0"
if "%MEMGB%"=="" set "MEMGB=0"

if %MEMGB% GEQ 51 goto :memory_ok
if %MEMGB% GEQ 29 goto :memory_tight

echo stack: Docker has about %MEMGB% GB of memory. This stack needs about 29 GB 1>&2
echo        just to hold the retrieval weights of its four model-loading 1>&2
echo        processes, so it would come up and then page instead of working. 1>&2
echo        Stopping here rather than after the build. 1>&2
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
echo Docker has about %MEMGB% GB of memory. Four processes here each load the
echo retrieval models, and the one measured figure is about 12 GB per process,
echo so expect this to be slow. Ingestion is usually what suffers first.
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
rem  services sharing one build context (this file has four -- otel-init,
rem  qdrant-ready, migrate and api all build `context: .` into
rem  agent-workbench:local) AND a non-ASCII directory name. One service with a
rem  non-ASCII name builds; four services under an ASCII name build; four under
rem  a non-ASCII name never do. COMPOSE_BAKE=false does not avoid it.
rem
rem  Plain `docker build` does not take that path and is unaffected -- verified
rem  against the same directory. A Windows checkout under a path like
rem  D:\projects\... is fine either way, and one under D:\Chinese-name\... is
rem  the common case here, so the two-step is unconditional rather than
rem  conditional on a check that would have to guess at the same rule.
echo Building the image. First run pulls Node 24, Python 3.12 and the
echo retrieval runtime, then downloads about 6.7 GB of model weights into a
echo named volume. Expect tens of minutes, once.
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
rem  This list used to be longer: before the image carried the embedding extra
rem  and the Workers started their own MCP servers, the honest sentence here
rem  was "no embedding runtime and no MCP servers". What is left is what a
rem  Linux container topology genuinely cannot assemble, plus the one thing
rem  nobody has typed yet -- and the second is the one that looks like neither
rem  a bug nor an absence, because a synthetic Worker takes a Task all the way
rem  to succeeded without ever calling a model or a tool.
rem
rem  Kept as a pointer rather than a full account. The System page names every
rem  absence and its remedy (ADR-102); this window only has to stop somebody
rem  from reading silence as completeness.
echo   Not everything is on. Without a provider key the Task Workers run
echo   SYNTHETIC handlers: tasks reach succeeded with no model call and no tool
echo   call. Sandbox execution and computer use are absent from any container
echo   topology. The console's System page lists what this stack did and did
echo   not assemble, why, and what to change. Save a key there, flip what you
echo   want, then: scripts\stack.cmd restart
echo.
rem  /ui/ rather than the bare root. The root answers 307 to the same place, so
rem  either works today. Naming the real path means this line does not depend
rem  on that redirect staying.
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
rem  the API and the Workers (ADR-101, ADR-103). Restart exactly those three.
rem  PostgreSQL, Qdrant and the collector keep running, so this takes seconds,
rem  where `down` and a fresh start would take the image build again.
docker compose --profile demo restart api task-worker task-worker-b
set "RC=%errorlevel%"
goto :popped

:popped
popd

:finish
if not "%RC%"=="0" if defined HOLD pause
exit /b %RC%
