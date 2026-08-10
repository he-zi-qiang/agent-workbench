# Runbook: every tool call is refused

Symptom: an agent proposes tools and each one comes back denied, with the run
finishing having done nothing.

This is the Tool Gateway applying an envelope narrower than the profile the
agent was built with. Read the refusal reason before widening anything: a
missing scope and an exceeded ceiling look identical from the transcript.
