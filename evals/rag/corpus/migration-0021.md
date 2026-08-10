# Migration 0021

Adds an objective preview column to the Task registry so a list of Tasks reads
as what they were asked to do rather than as identifiers.

The column is nullable: Tasks submitted before it existed keep showing their
id, which is legible, and backfilling from artifacts was rejected as a
migration that reads bytes.
