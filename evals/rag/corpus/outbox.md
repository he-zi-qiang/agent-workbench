# Handing work to a worker

An outbox claim is a lease with an expiry, not a possession, so a worker that
dies holding one does not take its share of the queue with it. Every claim
mints a fencing token, and an acknowledgement from a worker whose lease was
already reclaimed matches nothing and is refused.
