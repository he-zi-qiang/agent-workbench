# The aw-core cluster

`aw-core` is the PostgreSQL cluster this deployment treats as its system of
record. Rows are retained for four hundred days and then removed by the
retention job, which runs nightly and never deletes a row an open lease still
references.

Backups are taken at 02:00 UTC and restored into a scratch cluster once a
quarter, because a backup nobody has restored is a hypothesis.
