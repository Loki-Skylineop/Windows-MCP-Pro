"""Edit tool - surgical, validated, atomic file editing."""

import json
import os
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations
from platformdirs import user_desktop_dir

from windows_mcp.coding import edit_service
from windows_mcp.infrastructure import with_analytics

_DESCRIPTION = (
    "Surgical file editing. Keywords: edit, patch, diff, replace, str_replace, modify file, "
    "refactor, apply changes, insert lines, delete lines. Applies a validated batch of edits to "
    "one or more text files WITHOUT rewriting them - use this instead of 'FileSystem write' for "
    "any file that already exists.\n\n"
    "Safety guarantees: every edit in the batch is validated before a single byte is written "
    "(all-or-nothing), CRLF/LF and BOM are preserved, a timestamped backup of each modified file "
    "is kept, and 'expected_sha256' acts as a compare-and-swap guard so a file changed on disk is "
    "never clobbered. dry_run=true returns a unified diff and writes nothing.\n\n"
    "mode='apply' (default) takes edits=[{...}] where each object is:\n"
    "  file            path to edit (relative paths resolve against the Desktop)\n"
    "  mode            replace (default) | regex | lines | delete_lines | insert_after | "
    "insert_before | append | prepend | create | patch\n"
    "  old             text to find (replace/insert_*) or a Python regex (regex mode)\n"
    "  new             replacement / inserted / appended text\n"
    "  patch           patch mode only: a unified diff. Hunks are matched by their "
    "context, so stale line numbers are fine and one edit can carry many hunks\n"
    "  count           expected number of occurrences, default 1, or \"all\" for every match\n"
    "  start, end      1-based inclusive line range for lines/delete_lines\n"
    "  expected_sha256 optional precondition (full or short prefix, from mode='view')\n"
    "  dotall          regex mode only: let '.' match newlines\n"
    "  ignore_case     regex mode only\n"
    "  overwrite       create mode only: allow replacing an existing file\n\n"
    "If a match fails, the error reports the nearest matching lines and whether the text differs "
    "only by whitespace, so the next attempt can be exact.\n\n"
    "mode='view' returns a line-numbered slice of one file (path, start, end) plus its size, "
    "line count, EOL style, encoding and sha256 - read this before editing an unfamiliar file."
)


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve(path: str) -> str:
    """Absolute path; relative paths resolve against the Desktop like FileSystem does."""
    expanded = os.path.expanduser(os.path.expandvars(str(path)))
    if not os.path.isabs(expanded):
        expanded = os.path.join(user_desktop_dir(), expanded)
    return os.path.abspath(expanded)


def _coerce_edits(edits: Any) -> list[dict] | str:
    """Accept a list, a JSON string, or a single edit object; normalise the paths."""
    if edits is None:
        return "Error: 'edits' is required for mode='apply'."
    if isinstance(edits, str):
        text = edits.strip()
        if not text:
            return "Error: 'edits' is empty."
        try:
            edits = json.loads(text)
        except ValueError as exc:
            return f"Error: 'edits' is not valid JSON: {exc}"
    if isinstance(edits, dict):
        edits = edits.get("edits", [edits])
    if not isinstance(edits, list) or not edits:
        return "Error: 'edits' must be a non-empty list of edit objects."

    normalised: list[dict] = []
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return f"Error: edit #{index} is not an object."
        item = dict(edit)
        raw_path = item.pop("path", None) or item.get("file")
        if not raw_path:
            return f"Error: edit #{index} is missing 'file'."
        item["file"] = _resolve(raw_path)
        normalised.append(item)
    return normalised


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Edit",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Edit",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Edit-Tool")
    def edit_tool(
        mode: Literal["apply", "view"] = "apply",
        edits: Any = None,
        path: str = None,
        start: int = 1,
        end: int = None,
        dry_run: Any = False,
        backup: Any = True,
        numbers: Any = True,
        encoding: str = "utf-8",
        ctx: Context = None,
    ) -> str:
        action = str(mode or "apply").strip().lower()

        if action == "view":
            if not path:
                return "Error: 'path' is required for mode='view'."
            return edit_service.view(
                _resolve(path),
                start=int(start or 1),
                end=None if end in (None, 0) else int(end),
                encoding=encoding or "utf-8",
                numbers=_truthy(numbers, True),
            )

        if action != "apply":
            return "Error: mode must be 'apply' or 'view'."

        prepared = _coerce_edits(edits)
        if isinstance(prepared, str):
            return prepared
        return edit_service.apply_edits(
            prepared,
            dry_run=_truthy(dry_run, False),
            backup=_truthy(backup, True),
            encoding=encoding or "utf-8",
        )
