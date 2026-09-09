"""Surgical file editing for large codebases.

Why this module exists
----------------------
``FileSystem`` can only read a file or overwrite it completely. Rewriting a
5,000-line source file to change three lines is slow, burns the agent's context
budget, and silently destroys edits made concurrently in an IDE. This module
adds the primitives a coding agent actually needs:

* ``replace``        exact string replacement with match-count validation
* ``regex``          regular-expression replacement with capture groups
* ``lines``          replace an inclusive 1-based line range
* ``delete_lines``   remove an inclusive 1-based line range
* ``insert_after``   anchored insertion after an exact string
* ``insert_before``  anchored insertion before an exact string
* ``append`` / ``prepend``   add text to either end of a file
* ``create``         create a new file without ever silently clobbering one
* ``patch``          apply a unified diff, tolerating drifted line numbers

Guarantees
----------
1. Atomic batches - every edit is validated before a single byte is written, so
   one bad edit aborts the whole batch and leaves the tree untouched.
2. Encoding, BOM and line endings (CRLF/LF) are detected and preserved.
3. ``expected_sha256`` is a compare-and-swap precondition, so a file that
   changed on disk is never clobbered by a stale edit.
4. Timestamped backups of every modified file, so any edit can be undone.
5. ``dry_run`` returns a unified diff instead of touching the disk.
6. Failed matches return actionable hints (nearest lines, whitespace warnings)
   instead of a bare "not found".
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from platformdirs import user_data_dir

__all__ = [
    "apply_edits",
    "view",
    "file_digest",
    "BACKUP_DIR",
    "MAX_FILE_SIZE",
    "MODES",
]

# Refuse to edit anything larger than this (protects against binary blobs).
MAX_FILE_SIZE = 32 * 1024 * 1024
# Budget for unified diffs returned by dry runs.
MAX_DIFF_LINES = 240
# Budget for view().
MAX_VIEW_LINES = 1200

BACKUP_DIR = os.path.join(user_data_dir("windows-mcp", appauthor=False), "edit-backups")

MODES = (
    "replace",
    "regex",
    "lines",
    "delete_lines",
    "insert_after",
    "insert_before",
    "append",
    "prepend",
    "create",
    "patch",
)

_ANCHORED = ("replace", "regex", "insert_after", "insert_before")


def file_digest(path: str) -> str:
    """SHA-256 of the raw bytes on disk (matches ``Get-FileHash -Algorithm SHA256``)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _FileState:
    path: str
    text: str
    original: str
    eol: str
    bom: bool
    encoding: str
    existed: bool
    digest: str
    applied: int = 0


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_eol(raw: str) -> str:
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _restore(text: str, state: _FileState) -> bytes:
    out = text.replace("\n", state.eol) if state.eol != "\n" else text
    if state.bom:
        out = "\ufeff" + out
    return out.encode(state.encoding, errors="surrogatepass")


def _load(path: str, encoding: str, *, create: bool) -> _FileState | str:
    if not os.path.exists(path):
        if not create:
            return f"Error: file not found: {path}"
        # New files default to LF; that is what every toolchain and git prefers.
        return _FileState(
            path=path, text="", original="", eol="\n", bom=False,
            encoding=encoding, existed=False, digest="",
        )
    if os.path.isdir(path):
        return f"Error: {path} is a directory, not a file."

    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE:
        return f"Error: {path} is {size:,} bytes, over the {MAX_FILE_SIZE:,} byte edit limit."

    with open(path, "rb") as handle:
        blob = handle.read()
    digest = hashlib.sha256(blob).hexdigest()
    try:
        raw = blob.decode(encoding)
    except UnicodeDecodeError as exc:
        return f"Error: cannot decode {path} as {encoding}: {exc}"

    bom = raw.startswith("\ufeff")
    if bom:
        raw = raw[1:]
    return _FileState(
        path=path, text=_normalize(raw), original=_normalize(raw), eol=_detect_eol(raw),
        bom=bom, encoding=encoding, existed=True, digest=digest,
    )


def _atomic_write(state: _FileState) -> None:
    directory = os.path.dirname(state.path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(state.path)}.wmcp-{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(_restore(state.text, state))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, state.path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _backup(state: _FileState) -> str | None:
    if not state.existed:
        return None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{os.path.basename(state.path)}-{state.digest[:8]}.bak"
        target = os.path.join(BACKUP_DIR, name)
        with open(target, "wb") as handle:
            handle.write(_restore(state.original, state))
        return target
    except OSError:
        return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _not_found_hint(state: _FileState, needle: str) -> str:
    probe = next((line for line in needle.split("\n") if line.strip()), "")
    if not probe:
        return ""
    squashed = _squash(needle)
    if squashed and squashed in _squash(state.text):
        return (
            " Hint: that text exists but with different whitespace or indentation. "
            "Copy it verbatim from `Edit mode=view`."
        )
    lines = state.text.split("\n")
    stripped = [line.strip() for line in lines]
    close = difflib.get_close_matches(probe.strip(), stripped, n=3, cutoff=0.6)
    if not close:
        return " Hint: use `Grep` or `Edit mode=view` to copy the exact text first."
    spots = []
    for candidate in close:
        try:
            index = stripped.index(candidate) + 1
        except ValueError:
            continue
        spots.append(f"line {index}: {candidate[:100]}")
    return " Nearest matches -> " + " | ".join(spots) if spots else ""


def _as_count(value: Any) -> int | str | None:
    if isinstance(value, str) and value.strip().lower() == "all":
        return "all"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _line_bounds(edit: dict, total: int, label: str) -> tuple[int, int] | str:
    try:
        start = int(edit.get("start"))
    except (TypeError, ValueError):
        return f"Error: {label}: 'start' must be a 1-based line number."
    end_raw = edit.get("end", start)
    try:
        end = int(end_raw)
    except (TypeError, ValueError):
        return f"Error: {label}: 'end' must be a 1-based line number."
    if start < 1 or end < start:
        return f"Error: {label}: invalid range {start}-{end}."
    if start > total:
        return f"Error: {label}: start {start} is past the end of the file ({total} lines)."
    return start, min(end, total)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# How far a hunk may sit from the line number in its own header and still be
# applied: a diff is written against a snapshot, so by the time it arrives the
# numbers are routinely stale and only the surrounding context identifies it.
PATCH_FUZZ = 400


def _parse_hunks(patch: str) -> list[dict] | str:
    """Split a unified diff into hunks, or return a human-readable error."""
    hunks: list[dict] = []
    current: dict | None = None
    for raw in patch.rstrip("\n").split("\n"):
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if not match:
                return (
                    f"unrecognized hunk header {raw[:80]!r}; "
                    "expected '@@ -old,count +new,count @@'"
                )
            current = {"start": int(match.group(1)), "before": [], "after": []}
            hunks.append(current)
            continue
        if current is None:
            # "diff --git", "index ...", "--- a/x", "+++ b/x": preamble, not content.
            continue
        if raw.startswith("\\"):
            continue  # the "No newline at end of file" marker
        if raw.startswith("-"):
            current["before"].append(raw[1:])
        elif raw.startswith("+"):
            current["after"].append(raw[1:])
        elif raw.startswith(" ") or raw == "":
            # Plenty of tools strip the marker space from blank context lines.
            body = raw[1:] if raw else ""
            current["before"].append(body)
            current["after"].append(body)
        else:
            return (
                f"unrecognized diff line {raw[:80]!r}; every line inside a hunk must "
                "start with ' ', '-' or '+'"
            )
    if not hunks:
        return "no '@@' hunks found; pass a unified diff, e.g. from dry_run=true or `git diff`"
    return hunks


def _locate_hunk(lines: list[str], before: list[str], guess: int) -> int | None:
    """Find where *before* sits in *lines*, searching outwards from *guess*."""
    if not before:
        return max(0, min(len(lines), guess))
    span = len(before)
    limit = len(lines) - span
    if limit < 0:
        return None
    guess = max(0, min(guess, limit))
    for delta in range(0, PATCH_FUZZ + 1):
        for position in ((guess,) if delta == 0 else (guess + delta, guess - delta)):
            if 0 <= position <= limit and lines[position:position + span] == before:
                return position
    return None


def _hunk_hint(lines: list[str], before: list[str]) -> str:
    """Point at the lines a failed hunk was probably aimed at."""
    probe = next((line for line in before if line.strip()), "")
    if not probe:
        return ""
    stripped = [line.strip() for line in lines]
    spots = []
    for candidate in difflib.get_close_matches(probe.strip(), stripped, n=3, cutoff=0.6):
        try:
            spots.append(str(stripped.index(candidate) + 1))
        except ValueError:
            continue
    if spots:
        return (
            f" Context {probe.strip()[:60]!r} resembles line(s) {', '.join(spots)}; "
            "regenerate the diff from a fresh `Edit mode=view`."
        )
    return " Re-read the file with `Edit mode=view` and regenerate the diff."


def _apply_hunks(state: _FileState, hunks: list[dict], label: str) -> str | None:
    """Apply every hunk in order, tracking the drift each one causes for the next."""
    lines = state.text.split("\n")
    drift = 0
    for number, hunk in enumerate(hunks, start=1):
        before = hunk["before"]
        position = _locate_hunk(lines, before, hunk["start"] - 1 + drift)
        if position is None:
            return (
                f"Error: {label}: hunk #{number} (@@ -{hunk['start']} @@) does not match "
                f"the file.{_hunk_hint(lines, before)}"
            )
        lines[position:position + len(before)] = hunk["after"]
        drift += len(hunk["after"]) - len(before)
    state.text = "\n".join(lines)
    return None


def _apply_single(state: _FileState, edit: dict, index: int) -> str | None:
    mode = str(edit.get("mode") or "replace").strip().lower()
    label = f"edit #{index} ({mode}) on {state.path}"
    if mode not in MODES:
        return f"Error: {label}: unknown mode. Use one of: {', '.join(MODES)}."

    old = edit.get("old", edit.get("old_string"))
    new = edit.get("new", edit.get("new_string"))
    if new is None:
        new = edit.get("content")
    new = "" if new is None else _normalize(str(new))
    old = None if old is None else _normalize(str(old))

    if mode in _ANCHORED and not old:
        return f"Error: {label}: 'old' is required for this mode."

    count = _as_count(edit.get("count", 1))
    if count is None:
        return f"Error: {label}: 'count' must be a positive integer or \"all\"."

    if mode == "replace":
        occurrences = state.text.count(old)
        if occurrences == 0:
            return f"Error: {label}: text not found.{_not_found_hint(state, old)}"
        if count == "all":
            state.text = state.text.replace(old, new)
        else:
            if occurrences != count:
                return (
                    f"Error: {label}: found {occurrences} occurrences but expected {count}. "
                    f"Pass count={occurrences}, count=\"all\", or extend 'old' to make it unique."
                )
            state.text = state.text.replace(old, new, count)

    elif mode == "regex":
        flags = re.MULTILINE
        if edit.get("dotall") is True:
            flags |= re.DOTALL
        if edit.get("ignore_case") is True:
            flags |= re.IGNORECASE
        try:
            pattern = re.compile(old, flags)
        except re.error as exc:
            return f"Error: {label}: invalid regex: {exc}"
        matches = pattern.findall(state.text)
        if not matches:
            return f"Error: {label}: regex matched nothing."
        if count == "all":
            state.text = pattern.sub(new, state.text)
        else:
            if len(matches) != count:
                return (
                    f"Error: {label}: regex matched {len(matches)} times but expected {count}. "
                    f"Pass count={len(matches)} or count=\"all\"."
                )
            state.text = pattern.sub(new, state.text, count=count)

    elif mode in ("lines", "delete_lines"):
        lines = state.text.split("\n")
        bounds = _line_bounds(edit, len(lines), label)
        if isinstance(bounds, str):
            return bounds
        start, end = bounds
        if mode == "delete_lines" or new == "":
            replacement: list[str] = []
        else:
            replacement = new.split("\n")
        state.text = "\n".join(lines[: start - 1] + replacement + lines[end:])

    elif mode in ("insert_after", "insert_before"):
        occurrences = state.text.count(old)
        if occurrences == 0:
            return f"Error: {label}: anchor not found.{_not_found_hint(state, old)}"
        if count != "all" and occurrences != count:
            return (
                f"Error: {label}: anchor found {occurrences} times but expected {count}. "
                f"Pass count={occurrences}, count=\"all\", or extend the anchor."
            )
        times = -1 if count == "all" else count
        block = new.strip("\n")
        if mode == "insert_after":
            state.text = state.text.replace(old, old + "\n" + block, times)
        else:
            state.text = state.text.replace(old, block + "\n" + old, times)

    elif mode == "patch":
        patch_text = edit.get("patch") or edit.get("diff") or new
        if not str(patch_text).strip():
            return f"Error: {label}: 'patch' is required (a unified diff with '@@' hunks)."
        hunks = _parse_hunks(_normalize(str(patch_text)))
        if isinstance(hunks, str):
            return f"Error: {label}: {hunks}"
        failure = _apply_hunks(state, hunks, label)
        if failure:
            return failure

    elif mode == "append":
        joiner = "" if not state.text or state.text.endswith("\n") else "\n"
        state.text = state.text + joiner + new

    elif mode == "prepend":
        joiner = "" if new.endswith("\n") or not new else "\n"
        state.text = new + joiner + state.text

    elif mode == "create":
        if state.existed and edit.get("overwrite") is not True:
            return (
                f"Error: {label}: file already exists. Pass overwrite=true to replace it, "
                "or use mode 'replace' for a surgical edit."
            )
        state.text = new

    state.applied += 1
    return None


def _diff(state: _FileState) -> list[str]:
    return list(
        difflib.unified_diff(
            state.original.splitlines(keepends=False),
            state.text.splitlines(keepends=False),
            fromfile=f"a/{state.path}",
            tofile=f"b/{state.path}",
            lineterm="",
            n=2,
        )
    )


def apply_edits(
    edits: Any,
    *,
    dry_run: bool = False,
    encoding: str = "utf-8",
    backup: bool = True,
    max_diff_lines: int = MAX_DIFF_LINES,
) -> str:
    """Validate and apply a batch of edits atomically. Returns a text report."""
    if isinstance(edits, dict):
        edits = edits.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return "Error: 'edits' must be a non-empty list of edit objects."

    states: dict[str, _FileState] = {}
    order: list[str] = []

    # ---- validation pass: nothing is written until every edit succeeds ----
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return f"Error: edit #{index} is not an object."
        raw_path = edit.get("file") or edit.get("path")
        if not raw_path:
            return f"Error: edit #{index} is missing 'file'."
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(raw_path))))
        mode = str(edit.get("mode") or "replace").strip().lower()

        state = states.get(path)
        if state is None:
            loaded = _load(path, str(edit.get("encoding") or encoding), create=(mode == "create"))
            if isinstance(loaded, str):
                return f"{loaded} (edit #{index})"
            states[path] = state = loaded
            order.append(path)

        expected = edit.get("expected_sha256")
        if expected:
            expected = str(expected).strip().lower()
            if not state.digest.startswith(expected):
                return (
                    f"Error: edit #{index}: {path} has sha256 {state.digest[:16]} but you expected "
                    f"{expected[:16]} - the file changed on disk. Re-read it before editing."
                )

        failure = _apply_single(state, edit, index)
        if failure:
            return failure

    # ---- commit pass ----
    report: list[str] = []
    for path in order:
        state = states[path]
        if state.existed and state.text == state.original:
            report.append(f"NO-OP   {path} (edits produced no change)")
            continue

        diff = _diff(state)
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

        if dry_run:
            body = "\n".join(diff[:max_diff_lines])
            tail = "" if len(diff) <= max_diff_lines else f"\n... diff truncated ({len(diff)} lines total)"
            report.append(f"DRY-RUN {path} (+{added}/-{removed})\n{body}{tail}")
            continue

        backup_path = _backup(state) if backup else None
        _atomic_write(state)
        details = [
            f"{state.applied} edit(s)",
            f"{len(state.original):,} -> {len(state.text):,} chars",
            f"+{added}/-{removed} lines",
            f"sha256 {file_digest(path)[:16]}",
        ]
        if not state.existed:
            details.append("created")
        if backup_path:
            details.append(f"backup {backup_path}")
        report.append(f"OK      {path} :: " + ", ".join(details))

    header = (
        f"{'Dry run' if dry_run else 'Applied'}: {len(edits)} edit(s) across "
        f"{len(order)} file(s)"
    )
    return header + "\n" + "\n".join(report)


def view(
    path: str,
    *,
    start: int = 1,
    end: int | None = None,
    encoding: str = "utf-8",
    numbers: bool = True,
    max_lines: int = MAX_VIEW_LINES,
) -> str:
    """Return a line-numbered slice of a file plus the metadata needed to edit it."""
    loaded = _load(os.path.abspath(os.path.expanduser(os.path.expandvars(path))), encoding, create=False)
    if isinstance(loaded, str):
        return loaded
    lines = loaded.text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    first = max(1, int(start or 1))
    last = total if end is None else min(total, int(end))
    if first > total:
        return f"Error: start {first} is past the end of {loaded.path} ({total} lines)."
    if last < first:
        last = first
    if last - first + 1 > max_lines:
        last = first + max_lines - 1

    size = os.path.getsize(loaded.path)
    header = (
        f"File: {loaded.path}\n"
        f"Lines {first}-{last} of {total} | {size:,} bytes | "
        f"EOL {'CRLF' if len(loaded.eol) == 2 else 'LF'} | encoding {loaded.encoding}"
        f"{' | BOM' if loaded.bom else ''} | sha256 {loaded.digest[:16]}"
    )
    body = []
    for number in range(first, last + 1):
        text = lines[number - 1]
        body.append(f"{number:6d}| {text}" if numbers else text)
    footer = "" if last >= total else f"\n... {total - last} more line(s); pass start={last + 1} to continue"
    return header + "\n" + "\n".join(body) + footer
