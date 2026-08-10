# Configuration: coordination

`coordination.lease_duration_seconds` and
`coordination.heartbeat_interval_seconds` decide how long a claim survives
without a sign of life. `coordination.max_attempts` is what stops a poison unit
of work from being retried forever.

A heartbeat interval at or above the lease duration is a configuration that
guarantees expiry.
