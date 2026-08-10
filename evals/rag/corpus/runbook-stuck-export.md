# Runbook: an export that never produces a file

Symptom: a Task reports success on its draft, the reader was asked to confirm,
and no file appears afterwards.

Check the Approval Ledger first. In every occurrence so far the decision was
recorded against a version the graph had already moved past, so the export node
resumed, found no decision it could trust, and refused rather than exporting.
