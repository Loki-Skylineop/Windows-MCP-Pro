"""A shell runner built for coding work.

What this fixes compared to the plain ``PowerShell`` path
---------------------------------------------------------
1. **Working directory** - every call used to start in ``%USERPROFILE%``, so
   each command had to be prefixed with ``Set-Location``. ``cwd`` is now a
   first-class parameter.
2. **Persistent sessions** - each call spawned a brand new process, so ``cd``,
   ``$env:...`` and activated virtualenvs never survived. A ``session`` name now
   persists the working directory and environment variables between calls by
   snapshotting them after every command.
3. **Real exit codes** - the old status code came from the PowerShell host, so a
   failing ``npm run build`` frequently reported ``0`` while a harmless command
   reported ``1``. The command's own ``$LASTEXITCODE`` / ``$?`` is now
   propagated.
4. **Both streams** - stdout used to hide stderr whenever stdout was non-empty,
   which threw away compiler and linter diagnostics. Both are returned, clearly
   separated.
5. **Partial output on timeout** - a timeout used to discard everything the
   command had already printed. Whatever was captured before the kill is now
   returned with a clear banner.
6. **Long jobs** - the timeout ceiling is raised, and anything genuinely
   long-running should use the ``Job`` tool instead.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from platformdirs import user_data_dir

from windows_mcp.powershell.utils import run_with_graceful_timeout

__all__ = ["run", "ShellResult", "format_result", "SESSION_ROOT", "MAX_TIMEOUT"]

SESSION_ROOT = os.path.join(user_data_dir("windows-mcp", appauthor=False), "shell-sessions")
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 200_000
# Beyond this the encoded command no longer fits comfortably on a Windows
# command line, so the script is written to a temp file and run with -File.
ENCODED_COMMAND_LIMIT = 8000

_TIMEOUT_EXIT_CODE = 124

_PRELUDE = """$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:NO_COLOR = '1'
$env:FORCE_COLOR = '0'
"""


@dataclass
class ShellResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    cwd: str = ""
    timed_out: bool = False
    session: str | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


def _shell_path(shell: str | None = None) -> str:
    if shell:
        return shell
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def _prepare_env() -> dict:
    """Reuse the server's environment preparation when available."""
    try:
        from windows_mcp.powershell.service import _prepare_env as upstream

        prepared = upstream()
        if isinstance(prepared, dict) and prepared:
            return dict(prepared)
    except Exception:
        pass
    return dict(os.environ)


def _ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _session_file(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session)).strip("-")[:64] or "default"
    return os.path.join(SESSION_ROOT, f"{safe}.json")


def _load_session(session: str | None) -> dict:
    if not session:
        return {}
    try:
        with open(_session_file(session), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session(session: str, cwd: str, env_diff: dict) -> None:
    os.makedirs(SESSION_ROOT, exist_ok=True)
    payload = {"cwd": cwd, "env": env_diff, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(_session_file(session), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _decode(blob: bytes | str | None) -> str:
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    for encoding in ("utf-8", "cp1252", "cp866"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", errors="replace")


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2:]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n... [{dropped:,} characters omitted] ...\n{tail}", True


def _build_script(
    command: str,
    cwd: str,
    session: str | None,
    session_env: dict,
    state_path: str | None,
) -> str:
    parts = [_PRELUDE]
    for key, value in (session_env or {}).items():
        parts.append(f"$env:{key} = {_ps_literal(value)}")
    parts.append(f"Set-Location -LiteralPath {_ps_literal(cwd)}")
    parts.append("$global:__wm_code = 0")
    parts.append("try {")
    parts.append(str(command))
    parts.append("    $__wm_ok = $?")
    parts.append("    if ($null -ne $LASTEXITCODE) { $global:__wm_code = $LASTEXITCODE }")
    parts.append("    elseif (-not $__wm_ok) { $global:__wm_code = 1 }")
    parts.append("} catch {")
    parts.append("    $global:__wm_code = 1")
    parts.append("    [Console]::Error.WriteLine(($_ | Out-String).TrimEnd())")
    parts.append("} finally {")
    if session and state_path:
        parts.append("    $__wm_state = @{ cwd = (Get-Location).Path; env = @{} }")
        parts.append("    foreach ($__wm_item in Get-ChildItem Env:) { $__wm_state.env[$__wm_item.Name] = [string]$__wm_item.Value }")
        parts.append(
            "    ($__wm_state | ConvertTo-Json -Depth 3 -Compress) | "
            f"Set-Content -LiteralPath {_ps_literal(state_path)} -Encoding utf8"
        )
    else:
        parts.append("    $null = $global:__wm_code")
    parts.append("}")
    parts.append("exit $global:__wm_code")
    return "\n".join(parts)


def _argv(shell: str, script: str) -> tuple[list[str], str | None]:
    """Return the argv for *script*, falling back to a temp file when it is big."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    base = [shell, "-NoProfile", "-NonInteractive"]
    if len(encoded) <= ENCODED_COMMAND_LIMIT:
        return base + ["-EncodedCommand", encoded], None
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".ps1", delete=False, encoding="utf-8-sig", newline="\r\n"
    )
    try:
        handle.write(script)
    finally:
        handle.close()
    return base + ["-ExecutionPolicy", "Bypass", "-File", handle.name], handle.name


def run(
    command: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    session: str | None = None,
    shell: str | None = None,
) -> ShellResult:
    """Execute *command* and always return a structured result (never raises)."""
    result = ShellResult(session=session)
    if not command or not str(command).strip():
        result.stderr = "Error: 'command' is required."
        result.exit_code = 1
        return result

    try:
        timeout = int(timeout or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    stored = _load_session(session)
    session_env = stored.get("env") if isinstance(stored.get("env"), dict) else {}

    candidate = cwd or stored.get("cwd") or os.path.expanduser("~")
    candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(str(candidate))))
    if not os.path.isdir(candidate):
        result.notes.append(f"cwd {candidate} does not exist; falling back to the home directory")
        candidate = os.path.expanduser("~")
    result.cwd = candidate

    state_path = None
    if session:
        os.makedirs(SESSION_ROOT, exist_ok=True)
        state_path = _session_file(session) + ".state"

    script = _build_script(str(command), candidate, session, session_env, state_path)
    executable = _shell_path(shell)
    argv, temp_script = _argv(executable, script)
    base_env = _prepare_env()

    started = time.perf_counter()
    try:
        completed = run_with_graceful_timeout(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=candidate,
            env=base_env,
            check=False,
        )
        result.stdout = _decode(completed.stdout)
        result.stderr = _decode(completed.stderr)
        result.exit_code = int(completed.returncode or 0)
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        result.exit_code = _TIMEOUT_EXIT_CODE
        result.stdout = _decode(getattr(exc, "stdout", None))
        result.stderr = _decode(getattr(exc, "stderr", None))
        result.notes.append(
            f"Command exceeded the {timeout}s timeout and its process tree was killed. "
            "Partial output is preserved above. For long work use: Job mode=start command=..."
        )
    except Exception as exc:  # pragma: no cover - defensive
        result.exit_code = 1
        result.stderr = f"{type(exc).__name__}: {exc}"
    finally:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if temp_script:
            try:
                os.remove(temp_script)
            except OSError:
                pass

    # Persist session state (working directory + environment deltas).
    if session and state_path:
        try:
            with open(state_path, "r", encoding="utf-8-sig") as handle:
                snapshot = json.load(handle)
            os.remove(state_path)
        except (OSError, ValueError):
            snapshot = None
        if isinstance(snapshot, dict):
            new_cwd = str(snapshot.get("cwd") or candidate)
            captured = snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {}
            baseline = {str(key): str(value) for key, value in base_env.items()}
            diff = {
                str(key): str(value)
                for key, value in captured.items()
                if baseline.get(str(key)) != str(value)
                and str(key).upper() not in ("PYTHONIOENCODING", "PYTHONUTF8", "NO_COLOR", "FORCE_COLOR")
            }
            _save_session(session, new_cwd, diff)
            result.cwd = new_cwd
        else:
            _save_session(session, candidate, session_env)

    result.stdout, clipped_out = _clip(result.stdout)
    result.stderr, clipped_err = _clip(result.stderr)
    result.truncated = clipped_out or clipped_err
    return result


def format_result(result: ShellResult) -> str:
    """Render a :class:`ShellResult` for the MCP client.

    The first two lines keep the historical ``Response:`` / ``Status Code:``
    shape so existing prompts and parsers keep working; everything else is
    additive.
    """
    stdout = (result.stdout or "").rstrip()
    stderr = (result.stderr or "").rstrip()
    body = stdout if stdout else ""
    if stderr:
        body = f"{body}\n--- STDERR ---\n{stderr}" if body else f"--- STDERR ---\n{stderr}"
    if not body:
        body = "(no output)"

    meta = [f"Duration: {result.duration_ms} ms", f"CWD: {result.cwd}"]
    if result.session:
        meta.append(f"Session: {result.session}")
    if result.timed_out:
        meta.append("TIMED OUT (partial output)")
    if result.truncated:
        meta.append("output truncated")

    lines = [f"Response: {body}", f"Status Code: {result.exit_code}", " | ".join(meta)]
    lines.extend(f"Note: {note}" for note in result.notes)
    return "\n".join(lines)
