"""tools subpackage — registers all MCP tool definitions on a FastMCP instance.

Windows-MCP Pro deliberately ships a small tool surface: every tool description
is sent to the model on every single request, so a tool that is never called is
pure context tax. The GUI automation set (Snapshot, Screenshot, Click, Type,
Scroll, Move, Shortcut, MultiSelect, MultiEdit, DisplayInventory, WaitFor) was
removed; the desktop layer that powered it is still in the package for anyone who
wants to re-register those tools in a fork.
"""

from windows_mcp.tools import (
    app,
    clipboard,
    edit,
    filesystem,
    grep,
    jobs,
    notification,
    process,
    registry,
    search,
    shell,
    wait,
)

_MODULES = [
    shell,
    jobs,
    edit,
    grep,
    filesystem,
    search,
    app,
    process,
    clipboard,
    wait,
    notification,
    registry,
]


def register_all(mcp, *, get_desktop, get_analytics):
    """Register every tool module on *mcp*.

    *get_desktop* and *get_analytics* are zero-arg callables that return the
    current ``Desktop`` and ``PostHogAnalytics`` instances (resolved lazily so
    that tools can be registered before ``lifespan`` initializes the singletons).
    """
    for mod in _MODULES:
        mod.register(mcp, get_desktop=get_desktop, get_analytics=get_analytics)
