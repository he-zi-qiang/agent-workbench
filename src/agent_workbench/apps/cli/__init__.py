"""The local command line application.

A debugging, demonstration and evaluation surface that goes through the same
ports as every other entry point. It is the first process to run the walking
skeleton end to end: input, one scripted model turn, unified events, output.

Nothing is re-exported here on purpose. ``main`` is also a module run directly
by the console script, and importing it eagerly from the package would make
``python -m agent_workbench.apps.cli.main`` load it twice.
"""
