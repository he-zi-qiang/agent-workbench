# The wording of the durability guarantee

The contract published to operators reads, verbatim:

> Once an event has been acknowledged with a sequence, that sequence will never
> be reassigned, reordered, or skipped, and a subscriber holding it may resume
> from exactly that point for as long as the stream is retained.

Nothing about buffering, batching or replay changes those words. A deployment
that cannot honour them must not claim the guarantee, and the phrase to search
for when auditing a change is "never be reassigned, reordered, or skipped".
