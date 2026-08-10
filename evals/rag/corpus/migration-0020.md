# Migration 0020

Introduces knowledge bases as first-class rows, so a document belongs to a
named collection of documents rather than to a tenant directly.

Existing documents are moved into a default knowledge base by the migration
itself; nothing is left unassigned.
