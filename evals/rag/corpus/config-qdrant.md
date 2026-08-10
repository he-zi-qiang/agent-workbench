# Configuration: qdrant

`qdrant.read_alias` is what queries resolve through; `qdrant.write_collection`
is what ingestion fills. They may never be equal -- that is validated at
startup, because a rebuild needs somewhere to build that nobody is reading.

`qdrant.collection_schema_version` changes the index identity.
