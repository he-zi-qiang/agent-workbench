# Runbook: rebuilding an index generation

Symptom: retrieval returns points whose chunk boundaries no longer match the
documents they came from.

This is the Ingestion Worker having written points under a superseded index
identity. Build a new generation, verify it answers the gold set, then move the
alias; never edit points in place.
