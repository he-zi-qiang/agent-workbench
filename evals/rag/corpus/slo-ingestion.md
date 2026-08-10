# Ingestion targets

A document accepted for indexing should be retrievable within two minutes.
Nothing promises a shorter number for a small document: batching is what keeps
the embedding runtime from being reloaded per file.

A version that misses the target is not lost; it is late, and the outbox says
so.
