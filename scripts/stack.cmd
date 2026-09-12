@echo off
rem  Bring the whole stack up on Windows and open the console.
rem
rem  This is the Windows counterpart of `scripts/dev.sh` for the one job dev.sh
rem  cannot do here: dev.sh is bash, and it drives native processes that want
rem  uv, a Python 3.12 and a Node 24. This drives Compose instead, so the only
rem  thing the machine needs is Docker Desktop.
rem
rem      scripts\stack.cmd            build, start, wait for healthy, open the console
rem      scripts\stack.cmd lite       the same, without LibreOffice in the image
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

rem  Can the engine reach a registry? Asked here, before anything is spent,
rem  and only on the path that builds. `down`, `logs`, `status`, `restart`
rem  and `sandbox-image` have already branched away above.
rem
rem  The failure this exists for is not a network outage, it is a stale
rem  setting. Docker Desktop keeps a manual proxy of its own, and a Windows
rem  machine running Clash or v2ray usually has it pointed at that tool's
rem  local port. Upgrading the tool moves the port -- 7890 was the old
rem  default, 7897 is the current one -- and nothing tells Docker Desktop.
rem  The engine then dials a port with no listener on every registry
rem  request, and buildkit prints the same refusal four times over, once per
rem  base image, in a wall that names the proxy and never the setting that
rem  holds it. Measured on such a machine 2026-09-10; what it was reported
rem  as was "startup keeps failing".
rem
rem  `docker pull`, not `docker manifest inspect` and not a probe build. The
rem  pull happens in the ENGINE, which is the process Docker Desktop hands
rem  that proxy setting to. `manifest inspect` is a client-side registry
rem  call and reads HTTPS_PROXY out of this console instead -- so on the
rem  very machine that motivated this probe it succeeds while every build
rem  fails, which is worse than having no probe at all. hello-world is about
rem  2 kB and the pull is a no-op once it is in the local cache; failing, it
rem  took 0.23s against a build that took 20s to say the same thing.
docker pull --quiet hello-world:latest >nul 2>nul
if errorlevel 1 (
    echo stack: Docker's engine cannot reach a registry, so the build would 1>&2
    echo        die on its first line. Stopping here instead. 1>&2
    echo. 1>&2
    echo        The usual cause on Windows is Docker Desktop's own proxy 1>&2
    echo        setting naming a port nothing listens on -- a Clash or v2ray 1>&2
    echo        port that moved, 7890 in the older builds and 7897 in the 1>&2
    echo        current ones. Docker Desktop, Settings, Resources, Proxies: 1>&2
    echo        correct the port there, or put that page back on the system 1>&2
    echo        proxy, then Apply and restart. See docs/windows-quickstart.md. 1>&2
    if exist "%APPDATA%\Docker\settings-store.json" (
        echo. 1>&2
        echo        What Docker Desktop has saved right now: 1>&2
        findstr /i "ProxyHTTPMode OverrideProxyHTTP" "%APPDATA%\Docker\settings-store.json" 1>&2
    )
    echo. 1>&2
    echo        To read the error itself:  docker pull hello-world 1>&2
    echo        To build anyway:           scripts\stack.cmd anyway 1>&2
    if /i not "%~1"=="anyway" (
        set "RC=1"
        goto :popped
    )
    echo        Proceeding because you asked. 1>&2
)

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

rem  Disk, measured on the volume Docker actually writes to, for the same
rem  reason as the memory gate above: the alternative is tens of minutes of
rem  build followed by a failure that does not name its own cause.
rem
rem  What this stack costs was measured 2026-09-11, after one build that
rem  succeeded:
rem
rem      18.6 GB   the agent-workbench:local image. The embedding extra and
rem                LibreOffice are most of it.
rem       9.3 GB   the named volumes, of which about 6.7 GB is model weights.
rem      19.8 GB   build cache. Reclaimable AFTERWARDS, but it exists during
rem                the build, which is when the volume is tightest.
rem      -------
rem      48.5 GB   the docker_data.vhdx that came out of it.
rem
rem      30 GB   under this the built result does not fit even with the cache
rem              pruned, so the build cannot finish. Hard stop.
rem      50 GB   the measured figure rounded up. Under it the build can still
rem              run out while the cache is at its peak.
rem
rem  The failure this prevents does not look like a disk failure. Docker fills
rem  the volume, the build stops at `unpacking to ...` and stays there: no
rem  error, no exit, the log simply stops and the daemon begins answering 500
rem  to every API call. It reads as a hang. It cost most of two nights before
rem  anybody measured the volume instead of the image.
rem
rem  Measured through PowerShell rather than `dir`: the free-space line `dir`
rem  prints is localized, so parsing it fails on a non-English console, which
rem  is the common case here. PowerShell is on every supported Windows. The
rem  path is resolved through its reparse point first, because moving this
rem  directory to a roomier drive and leaving a junction behind is the usual
rem  remedy when the default location sits on a full system drive, and
rem  measuring the junction itself would report the wrong volume.
set "DISKGB="
for /f "tokens=*" %%g in ('powershell -NoProfile -Command "$p=Join-Path $env:LOCALAPPDATA 'Docker\wsl'; if(-not (Test-Path -LiteralPath $p)){$p=Join-Path $env:LOCALAPPDATA 'Docker'}; $i=Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue; if($i -and $i.Target){$p=$i.Target}; $d=[IO.Path]::GetPathRoot($p).Substring(0,1); [int][math]::Floor((Get-PSDrive $d).Free/1GB)" 2^>nul') do set "DISKGB=%%g"
if not defined DISKGB set "DISKGB=0"
if "%DISKGB%"=="" set "DISKGB=0"

if %DISKGB% GEQ 50 goto :disk_ok
if %DISKGB% GEQ 30 goto :disk_tight

echo stack: Docker's disk has about %DISKGB% GB free. One built image, its 1>&2
echo        volumes and the build cache measured 48.5 GB together, so this 1>&2
echo        build would fill the volume and then STOP at "unpacking" without 1>&2
echo        printing an error. Stopping here rather than there. 1>&2
echo. 1>&2
echo        Docker Desktop, Settings, Resources, Advanced, Disk image 1>&2
echo        location: point it at a drive with room. Docker Desktop moves 1>&2
echo        what is already there. See docs/windows-quickstart.md. 1>&2
echo. 1>&2
echo        To reclaim some in place:  docker system prune -a 1>&2
echo        To build anyway:           scripts\stack.cmd anyway 1>&2
if /i not "%~1"=="anyway" (
    set "RC=1"
    goto :popped
)
echo        Proceeding because you asked. 1>&2
goto :disk_ok

:disk_tight
echo Docker's disk has about %DISKGB% GB free. One built image plus its volumes
echo plus the build cache measured 48.5 GB, so this can still run out while the
echo cache is at its peak. Run `docker system prune -a` first if it does.
echo.

:disk_ok

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
rem  WITH_FIDELITY_PREVIEW=1: the image also carries headless LibreOffice
rem  and a CJK font, so a Word document on the Tasks page can be shown as a
rem  laid-out page and not only as extracted text (ADR-0045, ADR-0109).
rem
rem  The Dockerfile leaves that build argument off by default and says why:
rem  it is a several-hundred-megabyte download that has failed mid-build
rem  before, and a default image is not broken without it -- `GET
rem  /v1/artifacts/{id}/pdf` answers 503 and the console shows the text.
rem  That default is right for CI and for anybody typing `docker build`
rem  themselves. It is wrong for THIS file, whose one job since ADR-0105 is
rem  to assemble everything a container can: an image that already carries
rem  a multi-gigabyte retrieval runtime, and then shows a Word report as
rem  plain text because 700 MB was saved, reads as a console that cannot
rem  preview Word -- which is what a Windows user reported (2026-09-04).
rem  To build the lighter image on purpose: scripts\stack.cmd lite
set "FIDELITY=1"
if /i "%~1"=="lite" set "FIDELITY=0"

rem  The folder the API container may write on this machine (ADR-0109).
rem  compose.yaml binds var\projects at /projects and the profile opens the
rem  folder picker there. Created here, before `compose up`: on a Linux
rem  engine Docker would create a missing bind source as root, and then
rem  the non-root API could not write into it. Docker Desktop does not have
rem  that problem, but a launcher that only works on the engine it was
rem  tested on is the kind of launcher this file exists to replace.
if not exist "var\projects" mkdir "var\projects"

echo Building the image. First run pulls Node 24, Python 3.12, the Docker
echo CLI, LibreOffice and the retrieval runtime, then downloads about
echo 6.7 GB of model weights into a named volume. Expect tens of minutes,
echo once.
docker build --build-arg WITH_FIDELITY_PREVIEW=%FIDELITY% -t agent-workbench:local .
if errorlevel 1 (
    echo stack: image build failed -- see the output above. 1>&2
    rem  Stopping here matters. `compose up` would fall back to whatever
    rem  agent-workbench:local already exists and silently run a stale build.
    set "RC=1"
    goto :popped
)

rem  The browser image, and it is a second `docker build` rather than a
rem  `build:` in compose.yaml for two reasons that both had to be met.
rem
rem  Chromium plus the libraries it links against is a few hundred megabytes,
rem  and the shared image is what `api`, both Workers, `ingestion`, `sandbox`,
rem  `encoder` and `browser-egress` all run -- seven containers would carry a
rem  browser none of them can start (docker\browser.Dockerfile says the same
rem  at more length). And a `build:` under compose fails outright on a
rem  checkout whose path contains non-ASCII characters, with an error that
rem  never mentions the path; `docker build` does not.
rem
rem  It derives FROM the image built just above, so it must come after it.
echo Building the browser image (ADR-0113): Chromium on top of the image above.
docker build --build-arg BASE_IMAGE=agent-workbench:local -t agent-workbench-browser:local -f docker\browser.Dockerfile .
if errorlevel 1 (
    echo stack: browser image build failed -- see the output above. 1>&2
    rem  Same reasoning as the base image: `compose up` would otherwise reuse
    rem  a stale agent-workbench-browser:local, or fail on a missing image
    rem  with a message that says nothing about this step having been skipped.
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
echo   Coding sessions read and write folders under var\projects in this
echo   checkout (the only host folder the container can see); put a project
echo   there, or start with AGENT_WORKBENCH_PROJECTS_DIR set to another one.
echo   Their shell is the runner container, not this machine: Python 3.12
echo   and the usual Unix tools, no Node, nothing you installed on Windows,
echo   and every command stops on an approval card first. Their browser is
echo   the guarded Chromium in the browser container, not this desktop's.
echo   Both are probed once at start; the System page's Code rows say which
echo   answered.
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
rem
rem  All of which is true only when the rest of the stack IS running, and the
rem  most common moment to type this is the one moment it is not.
rem
rem  `docker compose restart` restarts the services it is given and does NOT
rem  start what they depend on. Against a stopped stack it therefore starts
rem  these four, leaves PostgreSQL, Qdrant and the encoder stopped, and exits
rem  0. The API comes up with no database, no vector store and no encoder
rem  behind it, the console answers nothing, and the launcher has just
rem  reported success. Measured 2026-09-11: exactly that, and what it got
rem  reported as was "restarting still fails".
rem
rem  A Windows reboot is what puts the stack in that state. Nothing here
rem  declares a restart policy, so when Docker Desktop comes back every
rem  long-running container is sitting in `Exited (255)` -- the code a
rem  container gets when the engine was stopped under it -- and none of them
rem  return on their own.
rem
rem  So check first, and refuse rather than half-start. Three services are
rem  probed rather than one because they fail independently and each is
rem  enough on its own to make the four below useless.
rem
rem  Compared inside `for /f` rather than with `findstr /x` against a dumped
rem  list. `findstr /x` anchors the whole line and counts the CR that Docker
rem  writes, so `postgres` never equals the line `postgres` and every probe
rem  reports missing -- a gate that refuses on a perfectly healthy stack,
rem  which is what the first version of this did. `for /f` strips the CR, and
rem  it needs no temporary file.
set "HAVE_PG="
set "HAVE_QD="
set "HAVE_EN="
for /f "usebackq delims=" %%s in (`docker compose --profile demo ps --status running --services 2^>nul`) do (
    if /i "%%s"=="postgres" set "HAVE_PG=1"
    if /i "%%s"=="qdrant" set "HAVE_QD=1"
    if /i "%%s"=="encoder" set "HAVE_EN=1"
)
set "NOTUP="
if not defined HAVE_PG set "NOTUP=1"
if not defined HAVE_QD set "NOTUP=1"
if not defined HAVE_EN set "NOTUP=1"
if defined NOTUP (
    echo stack: restart only restarts the four processes that read config once. 1>&2
    echo        PostgreSQL, Qdrant or the encoder is not running, so restarting 1>&2
    echo        those four would leave an API with nothing behind it -- and say 1>&2
    echo        it succeeded. This is what a Windows reboot leaves behind. 1>&2
    echo. 1>&2
    echo        Start the whole stack instead:  scripts\stack.cmd 1>&2
    echo        What is running right now:      scripts\stack.cmd status 1>&2
    set "RC=1"
    goto :popped
)

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
