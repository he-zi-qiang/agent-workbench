"""Application services.

A use case orchestrates ports and owns the order things happen in. It is the
layer an API route, a CLI command and a workflow node all call, so that the
rule "verify the stored object before the document exists" is written once
rather than once per entry point.
"""
