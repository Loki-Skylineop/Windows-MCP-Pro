"""Content search, repository mapping and symbol outlines.

Why this module exists
----------------------
``ripgrep`` is not installed on a stock Windows box and ``FileSystem mode=search``
only matches file *names*, so an agent that needs to find code by content has to
shell out to ``Select-String`` and pay the PowerShell start-up cost on every
query. These helpers run in-process instead, skip the directories nobody wants
to search (``.git``, ``node_modules``, ``__pycache__``, build output ...) and
return compact, line-numbered results that can be fed straight back into
``Edit``.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time

from windows_mcp.coding import ast_outline

__all__ = ["grep", "repo_map", "outline", "DEFAULT_SKIP_DIRS"]

DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vs", ".vscode", ".venv", "venv", "env",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "out", "target", ".next", ".nuxt", ".turbo", ".gradle",
    "bin", "obj", ".cache", ".terraform", "coverage", ".tox", ".eggs",
    "site-packages", ".angular", ".svelte-kit", ".parcel-cache",
})

BINARY_EXTENSIONS = frozenset({
    ".exe", ".dll", ".pdb", ".so", ".dylib", ".bin", ".obj", ".lib", ".a", ".o",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac", ".ogg", ".webm",
    ".zip", ".gz", ".7z", ".rar", ".tar", ".xz", ".zst", ".jar", ".whl", ".pyc",
    ".pdf", ".docx", ".xlsx", ".pptx", ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".sqlite", ".db", ".mdb", ".iso", ".dmg", ".msi", ".class", ".wasm",
})

DEFAULT_MAX_FILE_SIZE = 4 * 1024 * 1024
MAX_SCANNED_FILES = 40000
HARD_RESULT_CAP = 2000

_TS_PATTERNS = (
    # Real declarations: function / class / interface / type / enum / namespace.
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|namespace|module)\s+[\w$]+",
    # const/let/var only when they hold a function, an arrow, a class instance or
    # a multi-line object/array literal - plain value constants are noise.
    r"^\s*(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*"
    r"(?:async\s*)?(?:function\b|class\b|new\s+[\w$]+|\([^)]*\)\s*(?::[^=]+?)?=>"
    r"|[\w$]+\s*=>|\{\s*$|\[\s*$)",
    # Class members: methods, getters and setters.
    r"^\s*(?:public|private|protected|static|readonly|async|get|set)[\w\s]*[\w$]+\s*\([^;=]*\)\s*[:{]",
)

_SYMBOL_PATTERNS: dict[str, tuple[str, ...]] = {
    ".py": (r"^\s*(?:async\s+)?def\s+\w+", r"^\s*class\s+\w+", r"^\s*@\w[\w.]*"),
    ".pyi": (r"^\s*(?:async\s+)?def\s+\w+", r"^\s*class\s+\w+"),
    ".ts": _TS_PATTERNS,
    ".tsx": _TS_PATTERNS,
    ".js": _TS_PATTERNS,
    ".jsx": _TS_PATTERNS,
    ".mjs": _TS_PATTERNS,
    ".cjs": _TS_PATTERNS,
    ".go": (r"^func\s+", r"^type\s+\w+", r"^var\s+\w+", r"^const\s+"),
    ".rs": (r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|impl|mod|type|const|static)\s",),
    ".java": (r"^\s*(?:public|private|protected|static|final|abstract|synchronized)[\w\s<>\[\],.]*\w+\s*\(",
              r"^\s*(?:public|private|protected)?\s*(?:final\s+)?(?:class|interface|enum|record)\s+\w+"),
    ".cs": (r"^\s*(?:public|private|protected|internal|static|async|override|virtual|sealed|partial)[\w\s<>\[\],.?]*\w+\s*\(",
            r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:partial\s+)?(?:class|interface|struct|enum|record)\s+\w+"),
    ".kt": (r"^\s*(?:public|private|internal|open|override|suspend)?\s*(?:fun|class|object|interface|val|var)\s+\w+",),
    ".swift": (r"^\s*(?:public|private|internal|open|final)?\s*(?:func|class|struct|enum|protocol|extension|var|let)\s+\w+",),
    ".rb": (r"^\s*(?:def|class|module)\s+\w+",),
    ".php": (r"^\s*(?:abstract\s+|final\s+)?(?:public|private|protected|static)?\s*(?:function|class|interface|trait)\s+\w+",),
    ".c": (r"^[\w][\w\s\*]*\w+\s*\([^;]*\)\s*\{?\s*$", r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+\w+"),
    ".h": (r"^[\w][\w\s\*]*\w+\s*\([^;]*\)\s*;?\s*$", r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+\w+", r"^\s*#define\s+\w+"),
    ".cpp": (r"^[\w][\w\s\*:&<>,]*\w+\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$", r"^\s*(?:class|struct|enum|namespace)\s+\w+"),
    ".hpp": (r"^[\w][\w\s\*:&<>,]*\w+\s*\([^;]*\)\s*(?:const\s*)?;?\s*$", r"^\s*(?:class|struct|enum|namespace)\s+\w+"),
    ".ps1": (r"^\s*function\s+[\w-]+", r"^\s*class\s+\w+"),
    ".psm1": (r"^\s*function\s+[\w-]+",),
    ".sh": (r"^\s*(?:function\s+)?[\w-]+\s*\(\)\s*\{",),
    ".sql": (r"^\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|TRIGGER)\s+",),
    ".md": (r"^#{1,6}\s+\S",),
    ".yml": (r"^[\w.-]+:",),
    ".yaml": (r"^[\w.-]+:",),
    ".toml": (r"^\s*\[[^\]]+\]",),
}

_GENERIC_PATTERNS = (r"^\s*(?:function|class|def|fn|type|struct|interface|enum|module)\s+[\w$]+",)


def _resolve(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def _is_probably_binary(path: str) -> bool:
    if os.path.splitext(path)[1].lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(2048)
    except OSError:
        return True


def _iter_files(
    root: str,
    glob: str,
    skip_dirs: frozenset[str],
    include_hidden: bool,
    max_files: int,
):
    """Yield candidate file paths under *root*, honouring skip rules."""
    patterns = [part.strip() for part in str(glob or "*").split(",") if part.strip()] or ["*"]
    seen = 0
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name.lower() not in skip_dirs
            and (include_hidden or not name.startswith("."))
        ]
        for filename in filenames:
            if not any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                continue
            seen += 1
            if seen > max_files:
                return
            yield os.path.join(current, filename)


def grep(
    root: str,
    pattern: str,
    *,
    glob: str = "*",
    literal: bool = False,
    ignore_case: bool = False,
    context: int = 0,
    max_results: int = 200,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    include_hidden: bool = False,
    skip_dirs: frozenset[str] | None = None,
    multiline: bool = False,
) -> str:
    """Search file *contents* under *root* and return ripgrep-style output."""
    if not pattern:
        return "Error: 'pattern' is required."
    target = _resolve(root)
    if not os.path.exists(target):
        return f"Error: path not found: {target}"

    max_results = max(1, min(int(max_results or 200), HARD_RESULT_CAP))
    context = max(0, min(int(context or 0), 20))
    skips = frozenset(name.lower() for name in (skip_dirs or DEFAULT_SKIP_DIRS))

    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        return f"Error: invalid regex {pattern!r}: {exc}"

    candidates: list[str] = []
    if os.path.isfile(target):
        candidates = [target]
        base = os.path.dirname(target)
    else:
        base = target
        candidates = list(_iter_files(target, glob, skips, include_hidden, MAX_SCANNED_FILES))

    started = time.perf_counter()
    lines_out: list[str] = []
    total_matches = 0
    files_with_matches = 0
    scanned = 0
    skipped_big = 0
    truncated = False

    for path in candidates:
        if total_matches >= max_results:
            truncated = True
            break
        try:
            if os.path.getsize(path) > max_file_size:
                skipped_big += 1
                continue
        except OSError:
            continue
        if _is_probably_binary(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        scanned += 1

        file_lines = content.split("\n")
        hits: list[int] = []
        if multiline:
            for match in regex.finditer(content):
                hits.append(content.count("\n", 0, match.start()) + 1)
        else:
            for number, line in enumerate(file_lines, start=1):
                if regex.search(line):
                    hits.append(number)
        if not hits:
            continue

        files_with_matches += 1
        relative = os.path.relpath(path, base) if base else path
        previous_end = 0
        for number in hits:
            if total_matches >= max_results:
                truncated = True
                break
            start = max(1, number - context)
            end = min(len(file_lines), number + context)
            if context and previous_end and start > previous_end + 1:
                lines_out.append("--")
            for current in range(start, end + 1):
                text = file_lines[current - 1].rstrip("\r")
                if len(text) > 400:
                    text = text[:400] + " ...[line truncated]"
                separator = ":" if current == number else "-"
                lines_out.append(f"{relative}:{current}{separator} {text}")
            previous_end = end
            total_matches += 1

    elapsed = (time.perf_counter() - started) * 1000
    header = (
        f"Grep {'literal' if literal else 'regex'} {pattern!r} in {target} "
        f"(glob={glob}, case={'insensitive' if ignore_case else 'sensitive'}, context={context})"
    )
    summary = (
        f"Summary: {total_matches} match(es) in {files_with_matches} file(s); "
        f"scanned {scanned} file(s) in {elapsed:.0f} ms"
        + (f"; skipped {skipped_big} oversized file(s)" if skipped_big else "")
        + (f"; result cap {max_results} reached - narrow the pattern or raise max_results" if truncated else "")
    )
    if not lines_out:
        return f"{header}\nNo matches.\n{summary}"
    return f"{header}\n" + "\n".join(lines_out) + f"\n{summary}"


def repo_map(
    root: str,
    *,
    glob: str = "*",
    top: int = 30,
    include_hidden: bool = False,
    skip_dirs: frozenset[str] | None = None,
) -> str:
    """Summarise a tree: size/line totals, per-extension stats and the biggest files."""
    target = _resolve(root)
    if not os.path.isdir(target):
        return f"Error: not a directory: {target}"
    skips = frozenset(name.lower() for name in (skip_dirs or DEFAULT_SKIP_DIRS))
    top = max(1, min(int(top or 30), 200))

    started = time.perf_counter()
    per_extension: dict[str, list[int]] = {}
    biggest: list[tuple[int, int, str]] = []
    total_files = 0
    total_bytes = 0
    total_lines = 0

    for path in _iter_files(target, glob, skips, include_hidden, MAX_SCANNED_FILES):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        extension = os.path.splitext(path)[1].lower() or "<none>"
        lines = 0
        if extension not in BINARY_EXTENSIONS and size <= DEFAULT_MAX_FILE_SIZE:
            try:
                with open(path, "rb") as handle:
                    lines = handle.read().count(b"\n") + 1
            except OSError:
                lines = 0
        total_files += 1
        total_bytes += size
        total_lines += lines
        bucket = per_extension.setdefault(extension, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += size
        bucket[2] += lines
        biggest.append((lines, size, os.path.relpath(path, target)))

    biggest.sort(reverse=True)
    elapsed = (time.perf_counter() - started) * 1000

    out = [
        f"Repo map: {target} (glob={glob})",
        f"Totals: {total_files:,} file(s), {total_bytes:,} bytes, {total_lines:,} line(s) "
        f"[scanned in {elapsed:.0f} ms]",
        "",
        "By extension (count / bytes / lines):",
    ]
    ranked = sorted(per_extension.items(), key=lambda item: item[1][2], reverse=True)
    for extension, (count, size, lines) in ranked[:25]:
        out.append(f"  {extension:<12} {count:>6,}  {size:>12,}  {lines:>9,}")
    out.append("")
    out.append(f"Largest files by line count (top {min(top, len(biggest))}):")
    for lines, size, relative in biggest[:top]:
        out.append(f"  {lines:>7,} lines  {size:>11,} b  {relative}")
    return "\n".join(out)


DEFAULT_OUTLINE_SYMBOLS = 100
HARD_OUTLINE_SYMBOLS = 2000


def outline(
    path: str,
    *,
    encoding: str = "utf-8",
    max_symbols: int = DEFAULT_OUTLINE_SYMBOLS,
) -> str:
    """List the declarations in a source file with line numbers.

    Python files (``.py``/``.pyi``) are parsed with the real Python parser, so
    the nesting, the signatures and the line spans are exact and a ``def`` in a
    docstring is not mistaken for code. Every other language falls back to
    pattern matching. A Python file that does not parse is still outlined, with
    a note saying the result is pattern-matched.

    The cap is load-bearing: an outline of a generated 5,000-line file can
    otherwise return thousands of declarations and blow up the caller's
    context window. Anything hidden is reported as a count, so the caller
    knows the list is partial instead of silently trusting it.
    """
    target = _resolve(path)
    if not os.path.isfile(target):
        return f"Error: file not found: {target}"
    extension = os.path.splitext(target)[1].lower()
    try:
        with open(target, "r", encoding=encoding, errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        return f"Error: cannot read {target}: {exc}"

    try:
        limit = int(max_symbols or DEFAULT_OUTLINE_SYMBOLS)
    except (TypeError, ValueError):
        limit = DEFAULT_OUTLINE_SYMBOLS
    limit = max(1, min(limit, HARD_OUTLINE_SYMBOLS))

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline does not start a new line

    note = ""
    if extension in ast_outline.SUPPORTED_SUFFIXES:
        try:
            symbols = ast_outline.outline(content)
        except SyntaxError as exc:
            note = (
                f"Note: this file does not parse (line {exc.lineno or '?'}: {exc.msg}), "
                "so the list below is pattern-matched and may be wrong."
            )
        except (RecursionError, ValueError, MemoryError) as exc:
            note = (
                f"Note: the Python parser gave up ({type(exc).__name__}), "
                "so the list below is pattern-matched."
            )
        else:
            header = f"Outline: {target} ({len(lines)} lines, {extension}, python ast)"
            rows = [_format_symbol(item) for item in symbols[:limit]]
            return _finish_outline(header, rows, len(symbols))

    patterns = _SYMBOL_PATTERNS.get(extension, _GENERIC_PATTERNS)
    compiled = [re.compile(item) for item in patterns]

    found: list[str] = []
    matched = 0
    for number, line in enumerate(lines, start=1):
        if not any(regex.search(line) for regex in compiled):
            continue
        matched += 1
        if len(found) >= limit:
            continue
        text = line.rstrip()
        if len(text) > 200:
            text = text[:200] + " ..."
        found.append(f"{number:6d}| {text}")

    header = f"Outline: {target} ({len(lines)} lines, {extension or 'no extension'})"
    if note:
        header = f"{header}\n{note}"
    return _finish_outline(header, found, matched)


def _format_symbol(symbol: ast_outline.Symbol) -> str:
    """Render one parsed symbol: indent by nesting depth, append the line span."""
    text = symbol.text
    if symbol.end_line > symbol.line:
        text = f"{text}  [{symbol.line}-{symbol.end_line}]"
    text = "  " * symbol.depth + text
    if len(text) > 240:
        text = text[:240] + " ..."
    return f"{symbol.line:6d}| {text}"


def _finish_outline(header: str, rows: list[str], matched: int) -> str:
    """Join the rows and, when the cap bit, say exactly what was hidden."""
    if not rows:
        return f"{header}\nNo declarations matched. Use Grep for a custom pattern."
    body = header + "\n" + "\n".join(rows)
    if matched > len(rows):
        body += (
            f"\n... {matched - len(rows):,} more declaration(s) hidden: showing "
            f"{len(rows):,} of {matched:,}. Raise max_results (hard cap "
            f"{HARD_OUTLINE_SYMBOLS:,}) or use mode='grep' with a narrower pattern."
        )
    return body
