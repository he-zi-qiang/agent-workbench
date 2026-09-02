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
echo Building the image. First run pulls Node 24 and Python 3.12, so expect minutes.
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
rem  Said at the one moment somebody is looking at this window. This stack
rem  assembles Direct Chat and Tasks and *not* knowledge-base retrieval, web
rem  search, MCP tools or the sandbox -- and until ADR-102 nothing on screen
rem  said so, which is how a console that was working as configured came to be
rem  read as a broken provider key. The page names every absence and what it
rem  would take to fix it; this line is only the pointer to it.
echo   Not everything is on: this image has no embedding runtime and no MCP
echo   servers. The console's System page lists what this stack did or did not
echo   assemble, why, and what to change. Optional parts that are only a
echo   switch can be flipped there; then: scripts\stack.cmd restart
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
