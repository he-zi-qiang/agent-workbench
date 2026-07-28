# Refusal codes

`AWB-4471` is returned when a transfer's digest does not match the declared
upload. `AWB-4472` covers the size mismatch, and `AWB-4473` is the media type
this build cannot read. None of them retries: the client sent something other
than what it promised, and sending it again unchanged produces the same answer.

The gateway emits `AWB-5150` when a policy engine exceeds its bound, which is
distinct from `AWB-5151`, the code for a policy that raised.
