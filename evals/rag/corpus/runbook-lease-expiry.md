# Runbook: work that restarts forever

Symptom: the same unit of work is claimed, runs part way, and is claimed again
by another process minutes later.

This is the Task Worker failing to heartbeat within the lease window. Confirm
by comparing claim timestamps against the lease duration before changing any
configuration -- a lease that looks short is usually a heartbeat that stopped.
