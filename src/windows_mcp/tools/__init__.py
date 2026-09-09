"""tools subpackage — registers all MCP tool definitions on a FastMCP instance.

Windows-MCP Pro deliberately ships a small tool surface: every tool description
is sent to the model on every single request, so a tool that is never called is
pure context tax. The GUI automation set (Snapshot, Screenshot, Click, Type,
Scroll, Move, Shortcut, MultiSelect, MultiEdit, DisplayInventory, WaitFor) was
removed; the desktop layer that powered it is still in the package for anyone
who wants to re-register those tools in a fork.

Two cross-cutting behaviours live here rather than in the individual tool
modules, so that a newly added tool cannot forget them:

* **Category tags.** Descriptions are prefixed with ``[coding]``, ``[web]``,
  ``[windows]`` or ``[util]``. A flat list of tool names gives the model no
  grouping; the tag costs a couple of tokens per tool and makes the surface
  self-describing without introducing a separate profile mechanism.
* **Honest error flags.** Services report failures as a string starting with
  ``Error:``. That reads well, but the client was told ``isError: false``, so
  "file not found" arrived as a *successful* call: nothing retried, nothing was
  highlighted, and a model skimming a long reply could keep building on a
  result that never existed. Those payloads -- and ``ValueError``, which the
  services raise for bad arguments -- are re-raised as ``ToolError`` so FastMCP
  reports a protocol-level error with the message intact.
"""

from __future__ import annotations

import functools
import inspect

from fastmcp.exceptions import ToolError

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

CODING = "coding"
WEB = "web"
WINDOWS = "windows"
UTIL = "util"

CATEGORIES = (CODING, WEB, WINDOWS, UTIL)

#: Tool module -> category advertised in the description tag. Registration
#: order is the order the client lists them in, so the coding loop comes first.
_MODULES = (
    (shell, CODING),
    (jobs, CODING),
    (edit, CODING),
    (grep, CODING),
    (filesystem, CODING),
    (search, WEB),
    (app, WINDOWS),
    (process, WINDOWS),
    (clipboard, WINDOWS),
    (wait, UTIL),
    (notification, WINDOWS),
    (registry, WINDOWS),
)

ERROR_PREFIX = "Error:"


def tag_description(category: str, description: str | None) -> str | None:
    """Prefix *description* with ``[category]``; a no-op if already tagged."""
    if description is None:
        return None
    prefix = f"[{category}]"
    text = description.lstrip()
    if text.startswith(prefix):
        return description
    return f"{prefix} {text}"


def looks_like_failure(result: object) -> bool:
    """True when a service returned one of its ``Error: ...`` strings.

    Only the start of the payload counts. Output that merely *contains* the
    word further down -- a grep hit, a build log, a file being read back -- is a
    successful call and must not be flagged.
    """
    return isinstance(result, str) and result.lstrip().startswith(ERROR_PREFIX)


def surface_errors(fn):
    """Re-raise service failures as ``ToolError`` so clients see ``isError``.

    ``functools.wraps`` keeps ``__wrapped__`` pointing at the original callable,
    so FastMCP still derives the input schema from the real signature (including
    the injected ``Context`` parameter) even though the wrapper takes ``*args``.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if looks_like_failure(result):
                raise ToolError(result.strip())
            return result

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if looks_like_failure(result):
            raise ToolError(result.strip())
        return result

    return wrapper


class CategorisedMCP:
    """A FastMCP stand-in that tags descriptions and wraps tool callables.

    Every attribute except ``tool`` is forwarded untouched, so a tool module
    cannot tell the difference and keeps working if it reaches for another
    FastMCP API.
    """

    def __init__(self, mcp, category: str) -> None:
        self._mcp = mcp
        self._category = category

    def __getattr__(self, name: str):
        return getattr(self._mcp, name)

    @property
    def category(self) -> str:
        return self._category

    def tool(self, *args, **kwargs):
        # Bare ``@mcp.tool`` with no arguments: the description comes from the
        # docstring so there is nothing to tag, but the error wrapper applies.
        if args and callable(args[0]):
            return self._mcp.tool(surface_errors(args[0]), *args[1:], **kwargs)

        if "description" in kwargs:
            kwargs["description"] = tag_description(self._category, kwargs["description"])
        decorate = self._mcp.tool(*args, **kwargs)

        def register_tool(fn):
            return decorate(surface_errors(fn))

        return register_tool


def categories() -> dict[str, str]:
    """Module basename -> category. Used by the tests and the docs."""
    return {module.__name__.rsplit(".", 1)[-1]: category for module, category in _MODULES}


def register_all(mcp, *, get_desktop, get_analytics):
    """Register every tool module on *mcp*.

    *get_desktop* and *get_analytics* are zero-arg callables that return the
    current ``Desktop`` and ``PostHogAnalytics`` instances (resolved lazily so
    that tools can be registered before ``lifespan`` initializes the singletons).
    """
    for module, category in _MODULES:
        module.register(
            CategorisedMCP(mcp, category),
            get_desktop=get_desktop,
            get_analytics=get_analytics,
        )
