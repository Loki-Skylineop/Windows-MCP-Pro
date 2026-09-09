"""Web search / extraction stack behind the SearchPro tool.

``search_service`` runs inside the MCP server; ``worker.py`` is executed by the
separate interpreter that owns ddgs / trafilatura / scrapling / crawl4ai.
"""

from windows_mcp.websearch import search_service

__all__ = ["search_service"]
