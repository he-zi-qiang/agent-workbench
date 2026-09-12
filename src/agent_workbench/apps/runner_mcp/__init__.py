"""The command runner: one shell command per call, in a directory it was handed.

The sixth project-owned MCP server (ADR-0115), and the one with the least in
it. ``agent-sandbox-mcp`` starts a container per call; ``agent-browser-mcp``
drives a Chromium; this one calls ``/bin/sh -c`` with the same code the native
``project_run`` has always used (``adapters/filesystem/commands.py``) and
returns what happened. What makes it worth a process of its own is not what it
does but where it runs: a container that mounts the project directory and
holds no provider key, no database address and no artifact volume -- so the
shell a coding session is given under Compose is the shell F-37 said would be
acceptable, and not the one ADR-0109 §3.3 refused.
"""
