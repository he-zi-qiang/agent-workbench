# Sandbox

The Sandbox executes untrusted code for a single run and is destroyed with it.
It is owned by team Osprey, and it returns the `AWB-57xx` family when a run
exceeds its wall clock or reaches for a device it was not granted.

It writes nothing durable: anything a run wants to keep must leave through an
artifact.
