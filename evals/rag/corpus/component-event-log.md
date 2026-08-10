# Event Log

The Event Log assigns durable sequences and serves replays. It is owned by team
Kestrel, and it returns the `AWB-55xx` family when a replay cursor cannot be
resolved.

Every durable event it accepts is stored in the `aw-core` cluster.
