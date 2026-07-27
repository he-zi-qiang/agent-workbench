# Resuming a stream

Durable events receive a sequence assigned under their stream's row lock, which
makes the sequence gap-free rather than merely unique. A subscriber resumes by
sending the last identifier it saw, and receives everything after it. Transient
events carry no position and are never stored.
