# Runbook: context assembly is slow

Symptom: answers arrive, but the time before the first token grows with the
corpus rather than staying flat.

This is the Retrieval Service spending its budget on candidates it later
discards. Compare the candidate multiplier against how many survive
authorization before touching the model or the index.
