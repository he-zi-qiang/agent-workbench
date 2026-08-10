# Retrieval Service

The Retrieval Service narrows candidates, authorizes them, and builds the
context a model is shown. It is owned by team Kestrel, and it returns the
`AWB-52xx` family when a candidate cannot be authorized or a source moved.

It reads the `aw-vectors` cluster and writes nothing to it.
