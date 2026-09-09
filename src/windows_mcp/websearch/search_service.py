"""SearchPro service - web search and page extraction, out of process.

The tool layer (``windows_mcp.tools.search``) only validates and forwards; every
decision lives here so it can be unit tested without a network or a browser.

Design notes
------------

* **Separate interpreter.** The scraping stack (ddgs, trafilatura, scrapling,
  crawl4ai) is not a dependency of the server. It is discovered at runtime and
  driven through ``worker.py``. ``WINDOWS_MCP_SEARCH_PYTHON`` overrides discovery.
* **The bare ``python`` on PATH is a trap.** On this machine PATH resolves to a
  uv cache interpreter that reports the right version but has no packages, so
  candidates are probed for an importable ``ddgs`` before being accepted.
* **Timeouts are clamped.** The MCP client aborts a call at ~60s; a longer
  timeout would throw the whole answer away, so it is clamped and reported.
* **Output is budgeted.** Search results and page text are trimmed to a token
  budget with an explicit "raise max_chars" hint instead of silently flooding
  the context window.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SENTINEL = "__WMP_JSON__"
WORKER_PATH = Path(__file__).with_name("worker.py")

ENV_INTERPRETER = "WINDOWS_MCP_SEARCH_PYTHON"
ENV_CLIENT_TIMEOUT = "WINDOWS_MCP_CLIENT_TIMEOUT"
REQUIRED_MODULE = "ddgs"

LIST_MODES = ("search", "news", "images", "videos")
TEXT_MODES = ("read", "crawl")
MODES = LIST_MODES + TEXT_MODES + ("select", "env")

DEFAULT_MAX_RESULTS = 8
HARD_MAX_RESULTS = 50
DEFAULT_MAX_CHARS = 6000
HARD_MAX_CHARS = 120000
SNIPPET_LIMIT = 320

# Same ceiling as the PowerShell tool: the MCP client gives up at ~60s.
CLIENT_TIMEOUT_CEILING = 55
DEFAULT_TIMEOUT = {
    "search": 40,
    "news": 40,
    "images": 40,
    "videos": 40,
    "read": 45,
    "select": 45,
    "crawl": 55,
    "env": 45,
}

TIMELIMITS = ("d", "w", "m", "y")

# Ordered discovery hints. Expanded with os.path.expandvars before use.
INTERPRETER_HINTS = (
    r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe",
    r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe",
    r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe",
    r"%PROGRAMFILES%\Python312\python.exe",
    r"%PROGRAMFILES%\Python313\python.exe",
)

_interpreter_cache: str | None = None
_discovery_log: list[str] = []


class SearchStackMissing(RuntimeError):
    """No interpreter on this machine can import the search stack."""


def _no_window_kwargs() -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)  # never leak the server's src/ into the child
    return env


def _candidates() -> list[str]:
    found: list[str] = []

    override = (os.getenv(ENV_INTERPRETER) or "").strip().strip('"')
    if override:
        found.append(override)

    for hint in INTERPRETER_HINTS:
        expanded = os.path.expandvars(hint)
        if "%" not in expanded:
            found.append(expanded)

    for name in ("python3.12", "python3.13", "python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            found.append(resolved)

    found.append(sys.executable)

    unique: list[str] = []
    for path in found:
        if path and path not in unique:
            unique.append(path)
    return unique


def _has_module(interpreter: str, module: str = REQUIRED_MODULE) -> bool:
    probe = (
        "import importlib.util as u, sys; "
        f"sys.stdout.write('1' if u.find_spec({module!r}) else '0')"
    )
    try:
        completed = subprocess.run(
            [interpreter, "-c", probe],
            capture_output=True,
            text=True,
            timeout=25,
            env=_child_env(),
            **_no_window_kwargs(),
        )
    except Exception:
        return False
    return completed.stdout.strip().endswith("1")


def resolve_interpreter(force: bool = False) -> str:
    """Return the interpreter that owns the search stack.

    The result is cached: probing costs one subprocess per candidate, and the
    answer only changes when the user installs a new Python.
    """
    global _interpreter_cache, _discovery_log

    if _interpreter_cache and not force:
        return _interpreter_cache

    log: list[str] = []
    override = (os.getenv(ENV_INTERPRETER) or "").strip().strip('"')

    for candidate in _candidates():
        if not os.path.isfile(candidate) and not shutil.which(candidate):
            log.append(f"{candidate}: not found")
            continue
        if _has_module(candidate):
            _interpreter_cache = candidate
            _discovery_log = log + [f"{candidate}: OK"]
            return candidate
        log.append(f"{candidate}: no {REQUIRED_MODULE}")
        if candidate == override:
            _discovery_log = log
            raise SearchStackMissing(
                f"{ENV_INTERPRETER} points at {candidate}, but {REQUIRED_MODULE} is not "
                f"importable there. Install the stack:\n"
                f'  "{candidate}" -m pip install ddgs trafilatura "scrapling[fetchers]" crawl4ai'
            )

    _discovery_log = log
    raise SearchStackMissing(
        "No Python interpreter with the search stack was found. SearchPro runs out of "
        "process on the interpreter that has ddgs / trafilatura / scrapling / crawl4ai "
        "installed.\nTried:\n  "
        + "\n  ".join(log)
        + f"\nFix: set {ENV_INTERPRETER} to that interpreter, or install the stack with\n"
        '  <python.exe> -m pip install ddgs trafilatura "scrapling[fetchers]" crawl4ai'
    )


def discovery_log() -> list[str]:
    return list(_discovery_log)


def _client_ceiling() -> int:
    raw = os.getenv(ENV_CLIENT_TIMEOUT)
    if raw is None or not raw.strip():
        return CLIENT_TIMEOUT_CEILING
    try:
        value = int(float(raw))
    except ValueError:
        return CLIENT_TIMEOUT_CEILING
    return 0 if value <= 0 else value


def _clamp_timeout(mode: str, timeout: int | None) -> tuple[int, str | None]:
    requested = int(timeout) if timeout else DEFAULT_TIMEOUT.get(mode, 45)
    if requested <= 0:
        raise ValueError(f"timeout must be positive, got {requested}")
    ceiling = _client_ceiling()
    if ceiling and requested > ceiling:
        return ceiling, (
            f"Note: timeout={requested}s was clamped to {ceiling}s because the MCP client "
            f"aborts the call at ~60s. Run long crawls through the Job tool, or raise "
            f"{ENV_CLIENT_TIMEOUT}."
        )
    return requested, None


def _extract_reply(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        marker = line.find(SENTINEL)
        if marker >= 0:
            try:
                return json.loads(line[marker + len(SENTINEL) :])
            except json.JSONDecodeError:
                continue
    return None


def _run_worker(payload: dict, timeout: int) -> dict:
    """Execute worker.py in the discovered interpreter and return its reply."""
    interpreter = resolve_interpreter()
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="searchpro-", delete=False, encoding="utf-8"
    )
    try:
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        try:
            completed = subprocess.run(
                [interpreter, str(WORKER_PATH), handle.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_child_env(),
                **_no_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            partial = _extract_reply(exc.stdout or "") if exc.stdout else None
            if partial:
                partial["timed_out"] = True
                return partial
            raise TimeoutError(
                f"mode={payload.get('mode')} did not finish within {timeout}s. "
                "Narrow the query, lower max_results, or run it through the Job tool."
            ) from exc
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    reply = _extract_reply(completed.stdout)
    if reply is None:
        detail = (completed.stderr or completed.stdout or "").strip()
        tail = detail[-1200:] if detail else "no output"
        raise RuntimeError(
            f"search worker produced no parsable reply (exit {completed.returncode}).\n{tail}"
        )
    return reply


def _as_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def parse_selectors(selectors: object) -> dict[str, str]:
    """Accept a dict, a JSON object, or 'name=css; other=css' shorthand."""
    if selectors is None:
        return {}
    if isinstance(selectors, dict):
        return {str(key): str(value) for key, value in selectors.items()}
    if not isinstance(selectors, str):
        raise ValueError(f"selectors must be a mapping or string, got {type(selectors).__name__}")

    text = selectors.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"selectors is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("selectors JSON must be an object")
        return {str(key): str(value) for key, value in loaded.items()}

    parsed: dict[str, str] = {}
    for chunk in text.replace("\n", ";").split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(
                f"selector {piece!r} must look like name=css-selector, e.g. "
                "title=span.titleline > a::text"
            )
        name, _, selector = piece.partition("=")
        parsed[name.strip()] = selector.strip()
    return parsed


def _trim(text: str, limit: int) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    return text[:limit], (
        f"... trimmed: showing {limit:,} of {len(text):,} characters. "
        "Raise max_chars or narrow the request with focus=... to see more."
    )


def _format_list(reply: dict, mode: str, notes: list[str]) -> str:
    results = reply.get("results") or []
    header = (
        f"SearchPro mode={mode} engine={reply.get('engine', '?')} "
        f"results={len(results)} in {reply.get('elapsed', '?')}s"
    )
    if reply.get("query"):
        header += f" | query={reply['query']!r}"
    if reply.get("region"):
        header += f" region={reply['region']}"

    lines = [header]
    for index, row in enumerate(results, start=1):
        lines.append(f"{index:>2}. {row.get('title') or '(no title)'}")
        if row.get("url"):
            lines.append(f"    {row['url']}")
        extra = row.get("extra") or {}
        if extra:
            lines.append("    " + "  ".join(f"{key}={value}" for key, value in extra.items()))
        snippet = row.get("snippet") or ""
        if snippet:
            clipped = snippet[:SNIPPET_LIMIT]
            if len(snippet) > SNIPPET_LIMIT:
                clipped += "..."
            lines.append(f"    {clipped}")

    if not results:
        lines.append("(no results)")
    tried = [item for item in (reply.get("tried") or []) if item]
    if tried:
        lines.append("Backends tried before this: " + "; ".join(tried))
    lines.extend(notes)
    return "\n".join(lines)


def _format_text(reply: dict, mode: str, max_chars: int, notes: list[str]) -> str:
    text = reply.get("text") or ""
    body, trim_note = _trim(text, max_chars)
    header = (
        f"SearchPro mode={mode} engine={reply.get('engine', '?')} "
        f"url={reply.get('url', '?')} chars={reply.get('chars', len(text)):,} "
        f"in {reply.get('elapsed', '?')}s"
    )
    if reply.get("status_code") is not None:
        header += f" http={reply['status_code']}"
    lines = [header]
    if reply.get("blocked"):
        lines.append(
            "WARNING: this looks like a block/captcha page, not content. Retry with "
            "mode=crawl, or search for the same information instead of fetching this URL."
        )
    tried = [item for item in (reply.get("tried") or []) if item]
    if tried:
        lines.append("Fallbacks used: " + "; ".join(tried))
    lines.append("")
    lines.append(body if body.strip() else "(empty document)")
    if trim_note:
        lines.append("")
        lines.append(trim_note)
    lines.extend(notes)
    return "\n".join(lines)


def _format_select(reply: dict, notes: list[str]) -> str:
    rows = reply.get("rows") or []
    columns = reply.get("columns") or []
    lines = [
        f"SearchPro mode=select engine={reply.get('engine', '?')} "
        f"url={reply.get('url', '?')} rows={len(rows)} in {reply.get('elapsed', '?')}s",
        "columns: " + ", ".join(columns) if columns else "columns: (none)",
    ]
    for index, row in enumerate(rows, start=1):
        rendered = "  ".join(f"{key}={row.get(key, '')!r}" for key in columns)
        lines.append(f"{index:>3}. {rendered}")
    if not rows:
        lines.append("(nothing matched - the selectors are probably stale; check with mode=read)")
    lines.extend(notes)
    return "\n".join(lines)


def _format_env(reply: dict, notes: list[str]) -> str:
    lines = [
        "SearchPro mode=env",
        f"interpreter: {reply.get('interpreter')}",
        f"python: {reply.get('python')}",
        "packages:",
    ]
    for name, value in (reply.get("versions") or {}).items():
        lines.append(f"  {name}: {value}")
    probe = reply.get("probe") or {}
    if probe:
        lines.append("live probe:")
        for name, value in probe.items():
            lines.append(f"  {name}: {value}")
    log = discovery_log()
    if log:
        lines.append("discovery:")
        lines.extend(f"  {item}" for item in log)
    lines.extend(notes)
    return "\n".join(lines)


def run(
    *,
    mode: str = "search",
    query: str | None = None,
    url: str | None = None,
    max_results: int | None = None,
    region: str | None = None,
    timelimit: str | None = None,
    backend: str | None = None,
    selectors: object = None,
    max_chars: int | None = None,
    timeout: int | None = None,
    wait_for: str | None = None,
    js: str | None = None,
    scroll: bool | str = False,
    delay: float | None = None,
    focus: str | None = None,
    clean: bool | str = True,
) -> str:
    """Validate arguments, run the worker, and format the reply for the model."""
    normalised = (mode or "search").strip().casefold()
    if normalised not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}; got {mode!r}")

    if normalised in LIST_MODES and not (query or "").strip():
        raise ValueError(f"mode={normalised} requires query")
    if normalised in ("read", "crawl", "select") and not (url or "").strip():
        raise ValueError(f"mode={normalised} requires url")

    if timelimit and timelimit not in TIMELIMITS:
        raise ValueError(f"timelimit must be one of {', '.join(TIMELIMITS)}; got {timelimit!r}")

    notes: list[str] = []

    limit = int(max_results) if max_results else DEFAULT_MAX_RESULTS
    if limit < 1:
        raise ValueError(f"max_results must be positive, got {limit}")
    if limit > HARD_MAX_RESULTS:
        notes.append(f"Note: max_results={limit} clamped to {HARD_MAX_RESULTS}.")
        limit = HARD_MAX_RESULTS

    chars = int(max_chars) if max_chars else DEFAULT_MAX_CHARS
    if chars < 200:
        raise ValueError(f"max_chars must be at least 200, got {chars}")
    if chars > HARD_MAX_CHARS:
        notes.append(f"Note: max_chars={chars} clamped to {HARD_MAX_CHARS}.")
        chars = HARD_MAX_CHARS

    effective_timeout, timeout_note = _clamp_timeout(normalised, timeout)
    if timeout_note:
        notes.append(timeout_note)

    payload = {
        "mode": normalised,
        "query": (query or "").strip() or None,
        "url": (url or "").strip() or None,
        "max_results": limit,
        "region": (region or "wt-wt").strip(),
        "timelimit": timelimit,
        "backend": (backend or "").strip() or None,
        "selectors": parse_selectors(selectors),
        "fetch_timeout": max(5, effective_timeout - 5),
        "page_timeout": max(5000, (effective_timeout - 5) * 1000),
        "wait_for": (wait_for or "").strip() or None,
        "js": (js or "").strip() or None,
        "scroll": _as_bool(scroll, "scroll"),
        "delay": float(delay) if delay else None,
        "focus": (focus or "").strip() or None,
        "clean": _as_bool(clean, "clean"),
    }

    if normalised == "select" and not payload["selectors"]:
        raise ValueError(
            "mode=select requires selectors, e.g. "
            'selectors="title=span.titleline > a::text; url=span.titleline > a::attr(href)"'
        )

    reply = _run_worker(payload, effective_timeout)

    if reply.get("timed_out"):
        notes.append(f"Note: the worker hit the {effective_timeout}s deadline; output is partial.")

    if not reply.get("ok", False):
        detail = reply.get("error") or "unknown worker error"
        tried = [item for item in (reply.get("tried") or []) if item]
        if tried:
            detail += "\nTried: " + "; ".join(tried)
        raise RuntimeError(f"SearchPro mode={normalised} failed: {detail}")

    if normalised in LIST_MODES:
        return _format_list(reply, normalised, notes)
    if normalised in TEXT_MODES:
        return _format_text(reply, normalised, chars, notes)
    if normalised == "select":
        return _format_select(reply, notes)
    return _format_env(reply, notes)
