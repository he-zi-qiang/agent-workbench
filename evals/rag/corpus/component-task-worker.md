# Task Worker

The Task Worker claims queued Tasks, runs their graph, and settles them. It is
owned by team Northwind, and it returns the `AWB-54xx` family when a claim or a
settlement cannot proceed.

Its checkpoints and lifecycle rows live in the `aw-core` cluster.
