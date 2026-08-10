# Runbook: a subscriber cannot resume

Symptom: a client reconnects with the last identifier it saw and receives
nothing, or receives everything from the beginning.

This is the Event Log being handed a cursor from a stream it does not serve.
Verify the stream before assuming the sequence is at fault.
