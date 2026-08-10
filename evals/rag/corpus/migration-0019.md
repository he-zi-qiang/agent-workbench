# Migration 0019

Adds the tool execution ledger. Every admitted call gets a row keyed by its
operation, which is what makes a retried tool call idempotent rather than a
second effect.

Down is destructive: the ledger cannot be rebuilt from events alone.
