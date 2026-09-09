"""Detached background jobs for long-running commands.

Why this module exists
----------------------
``PowerShell`` is synchronous: the tool call has to return, so every build,
test run, ``npm install`` or ``pip install`` that takes longer than the timeout
is killed - and the old timeout path threw away everything the command had
already printed. ``Job`` moves that work out of the request/response cycle:

    Job start   -> spawn a detached PowerShell process, stream to a log file
    Job status  -> alive? exit code? runtime? last log line?
    Job logs    -> tail/head/grep the log while it runs
    Job stop    -> kill the process tree
    Job list    -> everything recent
    Job clean   -> prune old job folders

Each job lives in its own folder so nothing is lost when the MCP server
restarts:

    %LOCALAPPDATA%/windows-mcp/jobs/<job id>/
        command.ps1   the exact command that was requested
        runner.ps1    the wrapper that redirects output and records the exit code
        output.log    combined stdout+stderr, UTF-8, flushed line by line
        exit.code     written when the job finishes
        meta.json     command, cwd, pid, timestamps
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone

from platformdirs import user_data_dir

__all__ = ["start", "status", "logs", "stop", "list_jobs", "clean", "JOBS_ROOT"]

JOBS_ROOT = os.path.join(user_data_dir("windows-mcp", appauthor=False), "jobs")

# DETACHED_PROCESS is deliberately NOT used here.
#
# When the MCP server itself has no console (hidden window, service, hardened
# host), a fully detached powershell.exe is torn down before it executes a
# single statement: the job folder stays empty, no exit code is ever written and
# every job looks "lost". Measured on Windows 10/11 hosts - same runner.ps1 runs
# perfectly in the foreground, so only the creation flags were at fault.
#
# CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW gives what a background job needs:
# no visible window, no console popups, immunity to Ctrl+C aimed at the server,
# and the child keeps running after the tool call returns because we never wait
# on it and never inherit its handles.
_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)

_RUNNER_TEMPLATE = """$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:NO_COLOR = '1'
$env:FORCE_COLOR = '0'
$code = 0
$writer = New-Object System.IO.StreamWriter('__LOG__', $true, (New-Object System.Text.UTF8Encoding($false)))
$writer.AutoFlush = $true
try {
    Set-Location -LiteralPath '__CWD__'
    $writer.WriteLine('[windows-mcp] job __JOB__ started ' + (Get-Date -Format 'o'))
    $writer.WriteLine('[windows-mcp] cwd ' + (Get-Location).Path)
    & '__SCRIPT__' *>&1 | ForEach-Object { $writer.WriteLine(($_ | Out-String).TrimEnd()) }
    if ($null -ne $LASTEXITCODE) { $code = $LASTEXITCODE }
} catch {
    $code = 1
    $writer.WriteLine('[windows-mcp] terminating error:')
    $writer.WriteLine(($_ | Out-String).TrimEnd())
} finally {
    $writer.WriteLine('[windows-mcp] job finished with exit code ' + $code)
    $writer.Flush()
    $writer.Close()
    Set-Content -LiteralPath '__STATUS__' -Value $code -Encoding ascii
}
exit $code
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _shell_path(shell: str | None = None) -> str:
    if shell:
        return shell
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def _job_dir(job_id: str) -> str:
    return os.path.join(JOBS_ROOT, job_id)


def _sort_key(job_id: str) -> tuple[float, str]:
    """Order jobs by when they really started, never by their name.

    A job id only carries whole seconds, so two jobs started inside the same
    second would otherwise be ordered by their random hex suffix. Everything
    that means "the newest job" (the ``last`` alias, ``list``, ``status``
    without an id, prefix resolution, ``clean``) would then pick at random.
    """
    meta = _read_meta(job_id)
    stamp = meta.get("started_monotonic")
    if isinstance(stamp, (int, float)):
        return (float(stamp), job_id)
    started = meta.get("started_at")
    if isinstance(started, str):
        try:
            return (datetime.fromisoformat(started).timestamp(), job_id)
        except ValueError:
            pass
    try:
        return (os.path.getmtime(_job_dir(job_id)), job_id)
    except OSError:
        return (0.0, job_id)


def _all_job_ids() -> list[str]:
    if not os.path.isdir(JOBS_ROOT):
        return []
    entries = [name for name in os.listdir(JOBS_ROOT) if os.path.isdir(_job_dir(name))]
    entries.sort(key=_sort_key)
    return entries


def _resolve_job(job_id: str | None) -> str | None:
    ids = _all_job_ids()
    if not ids:
        return None
    if not job_id or str(job_id).lower() in ("last", "latest", "-1"):
        return ids[-1]
    job_id = str(job_id)
    if job_id in ids:
        return job_id
    matches = [candidate for candidate in ids if candidate.startswith(job_id) or job_id in candidate]
    return matches[-1] if matches else None


def _read_meta(job_id: str) -> dict:
    path = os.path.join(_job_dir(job_id), "meta.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _write_meta(job_id: str, meta: dict) -> None:
    path = os.path.join(_job_dir(job_id), "meta.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, ensure_ascii=False)


def _exit_code(job_id: str) -> int | None:
    path = os.path.join(_job_dir(job_id), "exit.code")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil

        process = psutil.Process(int(pid))
        return process.status() not in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE)
    except Exception:
        return False


def _decode_console(raw: bytes | None) -> str:
    """Decode output from a localised Windows console tool without crashing.

    ``taskkill`` answers in the OEM code page (cp866 on Russian Windows), so
    ``text=True`` would raise UnicodeDecodeError in a subprocess reader thread.
    """
    if not raw:
        return ""
    for codec in ("utf-8", "cp866", "cp1251", "cp1252"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_log(job_id: str) -> str:
    path = os.path.join(_job_dir(job_id), "output.log")
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _state(job_id: str) -> tuple[str, int | None]:
    code = _exit_code(job_id)
    if code is not None:
        return ("succeeded" if code == 0 else "failed"), code
    meta = _read_meta(job_id)
    if _pid_alive(meta.get("pid")):
        return "running", None
    return "lost (process gone, no exit code)", None


def _describe(job_id: str) -> str:
    meta = _read_meta(job_id)
    state, code = _state(job_id)
    log_path = os.path.join(_job_dir(job_id), "output.log")
    size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    log = _read_log(job_id)
    log_lines = log.count("\n")
    last = ""
    for line in reversed(log.split("\n")):
        if line.strip():
            last = line.strip()
            break
    started = meta.get("started_at", "?")
    runtime = ""
    if meta.get("started_monotonic"):
        if state == "running":
            runtime = f", running for {time.time() - float(meta['started_monotonic']):.0f}s"
        elif meta.get("finished_seconds"):
            runtime = f", took {float(meta['finished_seconds']):.0f}s"
    command = str(meta.get("command", "")).replace("\n", " ")
    if len(command) > 160:
        command = command[:160] + " ..."
    return (
        f"Job {job_id} [{state}{'' if code is None else f' exit={code}'}]\n"
        f"  command : {command}\n"
        f"  cwd     : {meta.get('cwd', '?')}\n"
        f"  pid     : {meta.get('pid', '?')}   started {started}{runtime}\n"
        f"  log     : {log_path} ({size:,} bytes, {log_lines} lines)\n"
        f"  last    : {last[:300] if last else '(no output yet)'}"
    )


def start(
    command: str,
    *,
    cwd: str | None = None,
    name: str | None = None,
    shell: str | None = None,
    env: dict | None = None,
) -> str:
    """Spawn *command* as a detached job and return its id immediately."""
    if not command or not str(command).strip():
        return "Error: 'command' is required."

    working_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(cwd))) if cwd else os.getcwd()
    if not os.path.isdir(working_dir):
        return f"Error: cwd not found: {working_dir}"

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name or "job")).strip("-")[:32] or "job"
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid.uuid4().hex[:6]}"
    directory = _job_dir(job_id)
    os.makedirs(directory, exist_ok=True)

    script_path = os.path.join(directory, "command.ps1")
    runner_path = os.path.join(directory, "runner.ps1")
    log_path = os.path.join(directory, "output.log")
    status_path = os.path.join(directory, "exit.code")

    # UTF-8 *with BOM*: Windows PowerShell 5.1 decodes a BOM-less .ps1 with the
    # ANSI code page, so Cyrillic/CJK/emoji in the command become mojibake and
    # usually a parse error. The BOM makes 5.1 and 7+ agree on UTF-8.
    with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(str(command))

    runner = (
        _RUNNER_TEMPLATE
        .replace("__LOG__", log_path.replace("'", "''"))
        .replace("__CWD__", working_dir.replace("'", "''"))
        .replace("__SCRIPT__", script_path.replace("'", "''"))
        .replace("__STATUS__", status_path.replace("'", "''"))
        .replace("__JOB__", job_id)
    )
    # Same BOM requirement as command.ps1: the runner embeds absolute paths,
    # and a user profile or repo directory can easily contain non-ASCII text.
    with open(runner_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(runner)

    child_env = dict(os.environ)
    if env:
        child_env.update({str(key): str(value) for key, value in env.items()})

    meta = {
        "job_id": job_id,
        "command": str(command),
        "cwd": working_dir,
        "started_at": _now(),
        "started_monotonic": time.time(),
        "log": log_path,
        "shell": _shell_path(shell),
    }

    try:
        process = subprocess.Popen(
            [_shell_path(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", runner_path],
            cwd=working_dir,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATION_FLAGS,
            close_fds=True,
        )
    except OSError as exc:
        return f"Error: failed to start job: {exc}"

    meta["pid"] = process.pid
    _write_meta(job_id, meta)

    # Never hand back an id for a process that died on the launch pad: wait a
    # moment for either the log file or a live process, otherwise report why.
    for _ in range(15):
        if os.path.exists(log_path) or process.poll() is None:
            break
        time.sleep(0.1)
    if process.poll() is not None and not os.path.exists(log_path):
        return (
            f"Error: job {job_id} exited immediately (code {process.poll()}) without "
            f"producing any output.\n"
            f"Runner: {runner_path}\n"
            f"Hint: run it in the foreground to see the failure: "
            f'{meta["shell"]} -NoProfile -ExecutionPolicy Bypass -File "{runner_path}"'
        )

    return (
        f"Started job {job_id} (pid {process.pid}) in {working_dir}\n"
        f"Log: {log_path}\n"
        f"Poll with: Job mode=status job_id={job_id}  |  Job mode=logs job_id={job_id} tail=80"
    )


def status(job_id: str | None = None) -> str:
    """Describe one job (default: the most recent one)."""
    resolved = _resolve_job(job_id)
    if not resolved:
        return "No jobs found. Start one with Job mode=start command=..."
    meta = _read_meta(resolved)
    code = _exit_code(resolved)
    if code is not None and not meta.get("finished_seconds") and meta.get("started_monotonic"):
        meta["finished_seconds"] = time.time() - float(meta["started_monotonic"])
        meta["finished_at"] = _now()
        _write_meta(resolved, meta)
    return _describe(resolved)


def logs(
    job_id: str | None = None,
    *,
    tail: int = 80,
    head: int | None = None,
    pattern: str | None = None,
    max_chars: int = 40000,
) -> str:
    """Return part of a job's log: last *tail* lines, first *head* lines, or grep."""
    resolved = _resolve_job(job_id)
    if not resolved:
        return "No jobs found."
    content = _read_log(resolved)
    if not content:
        state, _ = _state(resolved)
        return f"Job {resolved} [{state}] has produced no output yet."

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"
        selected = [f"{index:6d}| {line}" for index, line in enumerate(lines, 1) if regex.search(line)]
        label = f"grep {pattern!r}: {len(selected)} of {total} line(s)"
    elif head:
        head = max(1, min(int(head), total))
        selected = [f"{index:6d}| {line}" for index, line in enumerate(lines[:head], 1)]
        label = f"first {head} of {total} line(s)"
    else:
        tail = max(1, min(int(tail or 80), total))
        offset = total - tail
        selected = [f"{index:6d}| {line}" for index, line in enumerate(lines[offset:], offset + 1)]
        label = f"last {tail} of {total} line(s)"

    body = "\n".join(selected)
    if len(body) > max_chars:
        body = body[-max_chars:]
        label += " (truncated to fit)"
    state, code = _state(resolved)
    header = f"Job {resolved} [{state}{'' if code is None else f' exit={code}'}] - {label}"
    return header + "\n" + body


def stop(job_id: str | None = None) -> str:
    """Kill a job and its whole process tree."""
    resolved = _resolve_job(job_id)
    if not resolved:
        return "No jobs found."
    meta = _read_meta(resolved)
    pid = meta.get("pid")
    if not _pid_alive(pid):
        state, code = _state(resolved)
        return f"Job {resolved} is not running ({state}{'' if code is None else f', exit={code}'})."
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    status_path = os.path.join(_job_dir(resolved), "exit.code")
    if not os.path.exists(status_path):
        try:
            with open(status_path, "w", encoding="ascii") as handle:
                handle.write("-1")
        except OSError:
            pass
    output = (_decode_console(result.stdout) or _decode_console(result.stderr)).strip()
    return f"Stopped job {resolved} (pid {pid}). {output}"


def list_jobs(limit: int = 15) -> str:
    """List recent jobs, newest last."""
    ids = _all_job_ids()
    if not ids:
        return "No jobs found. Start one with Job mode=start command=..."
    limit = max(1, min(int(limit or 15), 100))
    selected = ids[-limit:]
    rows = [f"{len(ids)} job(s) total, showing {len(selected)} (root {JOBS_ROOT})", ""]
    for job_id in selected:
        meta = _read_meta(job_id)
        state, code = _state(job_id)
        command = str(meta.get("command", "")).replace("\n", " ")
        if len(command) > 90:
            command = command[:90] + " ..."
        rows.append(
            f"{job_id}  {state}{'' if code is None else f'({code})'}  pid={meta.get('pid', '?')}  {command}"
        )
    return "\n".join(rows)


def clean(keep: int = 10) -> str:
    """Delete all but the newest *keep* finished job folders."""
    ids = _all_job_ids()
    keep = max(0, int(keep or 0))
    removed = 0
    for job_id in ids[:-keep] if keep else ids:
        state, _ = _state(job_id)
        if state == "running":
            continue
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)
        removed += 1
    return f"Removed {removed} job folder(s); {len(_all_job_ids())} remaining in {JOBS_ROOT}."
