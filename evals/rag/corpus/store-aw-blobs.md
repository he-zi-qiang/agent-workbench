# The aw-blobs bucket

`aw-blobs` holds content-addressed bytes. Retention depends on what the bytes
are: run outputs are kept ninety days, while anything a human approved for
release is kept four hundred days.

Nothing is overwritten. A second upload of equal bytes resolves to the digest
that is already stored.
