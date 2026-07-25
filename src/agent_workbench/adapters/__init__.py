"""Concrete implementations of the ports.

This is the only layer allowed to know a vendor exists. Everything here
converts at its own edge, so the core keeps depending on the protocol rather
than on whatever library currently satisfies it.

The adapters shipped so far are deliberately dependency-free: a scripted model,
in-memory stores and side-effect-free tools. They make the contracts executable
before PostgreSQL, Qdrant or a real provider is involved, which is what keeps
continuous integration offline and deterministic.
"""
