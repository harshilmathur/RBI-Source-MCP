"""MCP tool implementations.

Each module here is a thin wrapper that takes parsed arguments + a DB
connection and returns a structured response dict. The MCP server in
`server.py` is the only place that knows about the MCP SDK; the tool
modules are pure functions over SQLite, which keeps them unit-testable
without spinning up an MCP harness.
"""
