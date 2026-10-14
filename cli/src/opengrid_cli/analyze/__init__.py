"""Local project analyzer for the `opengrid` CLI and the local MCP server.

Reads only allowlisted files inside the given root (see ``policy``), never executes or imports project
code, never touches the network, and redacts secret-looking values from everything it returns. The
output's ``requirements`` object follows WorkloadRequirements (docs/OPENGRID2_CONTRACTS.md §1); the
whole result follows §4.

    from opengrid_cli.analyze import analyze, plan
    result = analyze("path/to/project")
    preview = plan("path/to/project")      # dry run: what would be read, nothing opened
"""

from .engine import ANALYZER_VERSION, analyze, plan

__all__ = ["ANALYZER_VERSION", "analyze", "plan"]
