"""HTTP routes.

A route parses, resolves the caller, calls one application service and renders
the result. Anything it decides for itself is a rule that exists only over
HTTP, which is how a CLI and a workflow node end up with different behaviour
from the same system.
"""
