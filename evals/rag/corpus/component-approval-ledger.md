# Approval Ledger

The Approval Ledger records what a human authorized, and is the only thing an
export is allowed to read that answer from. It is owned by team Osprey, and it
returns the `AWB-56xx` family when a decision is replayed against a stale
version.

Its rows live in the `aw-core` cluster.
