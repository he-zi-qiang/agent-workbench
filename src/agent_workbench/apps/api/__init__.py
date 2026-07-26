"""The HTTP control plane.

Routes call application services and render what they return. The API is one
caller among several -- the CLI and, later, workflow nodes are others -- so a
rule that lives only in a route is a rule the other callers do not have.
"""
