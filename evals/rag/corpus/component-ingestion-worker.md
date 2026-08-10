# Ingestion Worker

The Ingestion Worker parses, chunks, embeds and indexes one document version at
a time. It is owned by team Marlin, and it returns the `AWB-53xx` family when a
document cannot be turned into points.

Everything it produces is written to the `aw-vectors` cluster.
