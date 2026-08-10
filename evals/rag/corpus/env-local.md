# Environment: local

Local development runs PostgreSQL on port 5433 rather than the default,
because a container published on 5432 is shadowed by whatever the machine is
already running, and the symptom is a confusing role error from a server nobody
started.

Local artifacts live under `var/`, which is not tracked.
