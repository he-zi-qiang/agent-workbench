# The aw-vectors cluster

`aw-vectors` is the Qdrant cluster holding embedded points. Index generations
are retired thirty days after the alias stops pointing at them, which is the
window a rollback has to work in.

There are no backups, deliberately: every point is derivable from a document
version and an index identity, so a restore is a rebuild.
