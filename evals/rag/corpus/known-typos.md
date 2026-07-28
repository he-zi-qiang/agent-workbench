# Spellings that appear in the wild

Support tickets frequently write `Qrdant` or `Qdrand` for Qdrant, and
`embeding` with one m. The ingestion log has historically emitted
`recieved chunk batch` -- the misspelling is preserved in older lines and a
search for the corrected spelling will not find them.

The configuration key `rag.embeding.model_id` never existed; a ticket quoting
it is quoting a typo, and the real key is `rag.embedding.model_id`.
