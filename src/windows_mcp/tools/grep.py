"""Grep tool - content search, repository maps and symbol outlines."""

import os
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations
from platformdirs import user_desktop_dir

from windows_mcp.coding import grep_service
from windows_mcp.infrastructure import with_analytics

_DESCRIPTION = (
    "Search code by CONTENT. Keywords: grep, ripgrep, search in files, find text, find usages, "
    "where is this used, regex search, repository map, outline, symbols, structure. "
    "'FileSystem mode=search' only matches file NAMES - this tool matches what is inside the "
    "files, in-process, with no PowerShell start-up cost, and it automatically skips .git, "
    "node_modules, __pycache__, .venv, dist, build, target and other noise directories.\n\n"
    "mode='grep' (default): regex (or literal=true) search under 'root'.\n"
    "  glob         comma-separated filename patterns, e.g. '*.py' or '*.ts,*.tsx'\n"
    "  context      lines of context around each hit (ripgrep -C)\n"
    "  ignore_case  case-insensitive search\n"
    "  multiline    let the pattern span line breaks\n"
    "  max_results  hit cap, default 200\n"
    "  Output is 'path:line: text', ready to paste into Edit.\n\n"
    "mode='map': size/line totals for a tree, per-extension breakdown and the largest files - "
    "the fastest way to understand an unfamiliar repository.\n\n"
    "mode='outline': list the declarations (functions, classes, types, headings) of one file with "
    "line numbers, so a 5,000-line file can be navigated without reading it all. The list is "
    "capped at 'max_results' declarations (default 100, hard cap 2000) and reports how many were "
    "hidden, so a generated file cannot flood the context."
)


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve(path: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(str(path or ".")))
    if not os.path.isabs(expanded):
        expanded = os.path.join(user_desktop_dir(), expanded)
    return os.path.abspath(expanded)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Grep",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Grep",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Grep-Tool")
    def grep_tool(
        mode: Literal["grep", "map", "outline"] = "grep",
        root: str = ".",
        pattern: str = None,
        glob: str = "*",
        literal: Any = False,
        ignore_case: Any = False,
        context: int = 0,
        max_results: int = None,
        multiline: Any = False,
        include_hidden: Any = False,
        top: int = 30,
        path: str = None,
        ctx: Context = None,
    ) -> str:
        action = str(mode or "grep").strip().lower()

        if action == "outline":
            target = path or root
            if not target:
                return "Error: 'path' is required for mode='outline'."
            return grep_service.outline(
                _resolve(target),
                max_symbols=int(max_results or grep_service.DEFAULT_OUTLINE_SYMBOLS),
            )

        if action == "map":
            return grep_service.repo_map(
                _resolve(root),
                glob=glob or "*",
                top=int(top or 30),
                include_hidden=_truthy(include_hidden, False),
            )

        if action != "grep":
            return "Error: mode must be 'grep', 'map' or 'outline'."

        if not pattern:
            return "Error: 'pattern' is required for mode='grep'."
        return grep_service.grep(
            _resolve(path or root),
            pattern,
            glob=glob or "*",
            literal=_truthy(literal, False),
            ignore_case=_truthy(ignore_case, False),
            context=int(context or 0),
            max_results=int(max_results or 200),
            include_hidden=_truthy(include_hidden, False),
            multiline=_truthy(multiline, False),
        )
