"""Git service: status, diffs, history, commits, branches and setup info.

``PowerShell`` can already run git, so a dedicated tool has to earn its schema:

* **Output budget.** ``git diff`` on a real change is thousands of lines, and
  through the shell tool all of them land in the model's context. Every mode
  here trims and says what it trimmed.
* **The identity trap.** A fresh Windows box has no ``user.email``, so the first
  commit an agent attempts dies with "Author identity unknown" - work done,
  nothing saved. ``commit`` falls back to the previous commit's author, then to
  a local identity, and reports which one it used.
* **One question, one call.** "Which repositories do I have, and is GitHub
  connected?" is ``mode=info`` instead of five shell invocations.

Deliberately absent: push, reset, rebase, cherry-pick and anything else that
rewrites history or touches a remote. Those stay in ``PowerShell``, where the
exact command is visible before it runs.
"""

from __future__ import annotations

import functools
import os
import subprocess

DEFAULT_TIMEOUT = 20
DEFAULT_LOG_LIMIT = 15
HARD_LOG_LIMIT = 200
DEFAULT_DIFF_LINES = 200
HARD_DIFF_LINES = 4000
DEFAULT_STATUS_FILES = 40
DEFAULT_BRANCHES = 30
DEFAULT_SCAN_DEPTH = 2
DEFAULT_SCAN_CAP = 20
GH_STATUS_LINES = 10

FALLBACK_NAME = "windows-mcp"
FALLBACK_EMAIL = "windows-mcp@localhost"

SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "venv",
    "dist",
    "build",
    "target",
    "out",
    "AppData",
}

_IDENTITY_MARKERS = (
    "author identity unknown",
    "please tell me who you are",
    "empty ident name",
    "unable to auto-detect email address",
)


class GitError(RuntimeError):
    """A git invocation that could not run, or a request that makes no sense."""


def _no_window_kwargs() -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def _run_exe(
    exe: str,
    args: list[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """Run *exe* and return (returncode, stdout, stderr), both streams stripped."""
    try:
        completed = subprocess.run(
            [exe, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        raise GitError(f"{exe} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{exe} {' '.join(args)} did not finish within {timeout}s") from exc
    return completed.returncode, (completed.stdout or "").strip(), (completed.stderr or "").strip()


def _git(args: list[str], cwd: str | None, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    return _run_exe("git", args, cwd=cwd, timeout=timeout)


def _reports_errors(fn):
    """Turn GitError into the ``Error: ...`` string every tool speaks."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GitError as exc:
            return f"Error: {exc}"

    return wrapper


def _resolve_cwd(path: str) -> str:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "."))))
    if os.path.isfile(target):
        target = os.path.dirname(target)
    if not os.path.isdir(target):
        raise GitError(f"path not found: {target}")
    return target


def _repo_root(path: str) -> str:
    cwd = _resolve_cwd(path)
    code, out, err = _git(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out:
        raise GitError(f"{cwd} is not inside a git repository ({err or 'no .git directory found'})")
    return os.path.normpath(out.splitlines()[0])


def _last_commit(root: str) -> str:
    code, out, _ = _git(["log", "-1", "--format=%h %s (%an, %ar)"], root)
    return out if code == 0 and out else "(no commits yet)"


def _config_value(key: str, cwd: str) -> str:
    code, out, _ = _git(["config", "--get", key], cwd)
    return out.strip() if code == 0 else ""


def _trim_lines(text: str, limit: int) -> tuple[str, str | None]:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, None
    return "\n".join(lines[:limit]), (
        f"... trimmed: showing {limit} of {len(lines)} lines. "
        "Raise max_lines, or pass file=... to diff one path."
    )


@_reports_errors
def status(path: str = ".", *, limit: int = DEFAULT_STATUS_FILES) -> str:
    """Branch, upstream, last commit and changed files grouped by stage."""
    root = _repo_root(path)
    code, out, err = _git(["status", "--porcelain=v1", "--branch"], root)
    if code != 0:
        raise GitError(err or "git status failed")

    lines = out.splitlines()
    branch = lines[0][3:].strip() if lines and lines[0].startswith("## ") else "(unknown)"

    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for entry in lines[1:]:
        if len(entry) < 4:
            continue
        index_state, work_state, name = entry[0], entry[1], entry[3:]
        if index_state == "?" and work_state == "?":
            untracked.append(name)
            continue
        if index_state.strip():
            staged.append(f"{index_state} {name}")
        if work_state.strip():
            unstaged.append(f"{work_state} {name}")

    report = [f"Repo: {root}", f"Branch: {branch}", f"Last commit: {_last_commit(root)}"]
    for title, group in (("Staged", staged), ("Unstaged", unstaged), ("Untracked", untracked)):
        if not group:
            continue
        report.append("")
        report.append(f"{title} ({len(group)}):")
        report.extend(f"  {item}" for item in group[:limit])
        if len(group) > limit:
            report.append(f"  ... and {len(group) - limit} more")

    if not (staged or unstaged or untracked):
        report.append("")
        report.append("Working tree clean.")
    return "\n".join(report)


@_reports_errors
def diff(
    path: str = ".",
    *,
    staged: bool = False,
    target: str | None = None,
    file: str | None = None,
    max_lines: int = DEFAULT_DIFF_LINES,
) -> str:
    """A --stat summary plus the patch itself, trimmed to *max_lines*."""
    root = _repo_root(path)
    limit = max(20, min(int(max_lines or DEFAULT_DIFF_LINES), HARD_DIFF_LINES))

    base = ["diff"]
    if staged:
        base.append("--staged")
    if target:
        base.append(str(target))
    tail = ["--", str(file)] if file else []

    code, summary, err = _git([*base, "--stat", *tail], root)
    if code != 0:
        raise GitError(err or "git diff failed")
    code, patch, err = _git([*base, *tail], root)
    if code != 0:
        raise GitError(err or "git diff failed")

    if not patch.strip():
        scope = "staged" if staged else "unstaged"
        where = f" in {file}" if file else ""
        return f"Repo: {root}\nNo {scope} changes{where}."

    body, note = _trim_lines(patch, limit)
    report = [f"Repo: {root}", summary or "(no summary)", "", body]
    if note:
        report.extend(["", note])
    return "\n".join(report)


@_reports_errors
def log(
    path: str = ".",
    *,
    limit: int = DEFAULT_LOG_LIMIT,
    file: str | None = None,
    pattern: str | None = None,
    target: str | None = None,
) -> str:
    """Recent commits, one line each."""
    root = _repo_root(path)
    count = max(1, min(int(limit or DEFAULT_LOG_LIMIT), HARD_LOG_LIMIT))

    args = ["log", f"-n{count}", "--date=relative", "--format=%h  %ad  %an  %s"]
    if pattern:
        args.extend(["-i", "--grep", str(pattern)])
    if target:
        args.append(str(target))
    if file:
        args.extend(["--", str(file)])

    code, out, err = _git(args, root)
    if code != 0:
        raise GitError(err or "git log failed")
    if not out.strip():
        return f"Repo: {root}\n(no commits match)"
    return f"Repo: {root}\n{out}"


def _identity_missing(stderr: str) -> bool:
    lowered = (stderr or "").casefold()
    return any(marker in lowered for marker in _IDENTITY_MARKERS)


def _fallback_identity(root: str) -> tuple[str, str]:
    """Reuse the previous commit's author before inventing one."""
    code, out, _ = _git(["log", "-1", "--format=%an%x1f%ae"], root)
    if code == 0 and "\x1f" in out:
        name, _, email = out.partition("\x1f")
        if name.strip() and email.strip():
            return name.strip(), email.strip()
    return FALLBACK_NAME, FALLBACK_EMAIL


@_reports_errors
def commit(
    path: str = ".",
    *,
    message: str | None = None,
    add_all: bool = True,
    paths: str | None = None,
) -> str:
    """Stage and commit. Never pushes."""
    if not str(message or "").strip():
        raise GitError("mode=commit requires a message")
    root = _repo_root(path)

    if paths:
        targets = [item.strip() for item in str(paths).split(",") if item.strip()]
        code, _, err = _git(["add", "--", *targets], root)
    elif add_all:
        code, _, err = _git(["add", "-A"], root)
    else:
        code, err = 0, ""
    if code != 0:
        raise GitError(err or "git add failed")

    code, staged_names, err = _git(["diff", "--cached", "--name-only"], root)
    if code != 0:
        raise GitError(err or "git diff --cached failed")
    if not staged_names.strip():
        raise GitError("nothing staged to commit; pass add_all=true or name files in paths=...")

    code, out, err = _git(["commit", "-m", str(message)], root)
    if code != 0 and _identity_missing(err or out):
        name, email = _fallback_identity(root)
        code, out, err = _git(
            ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", str(message)],
            root,
        )
        if code == 0:
            out = f"{out}\nNote: no git identity is configured; committed as {name} <{email}>."
    if code != 0:
        raise GitError(err or out or "git commit failed")

    files = len([line for line in staged_names.splitlines() if line.strip()])
    return f"Repo: {root}\n{out}\n({files} file(s) staged for this commit; not pushed)"


@_reports_errors
def branch(
    path: str = ".",
    *,
    name: str | None = None,
    create: bool = False,
    limit: int = DEFAULT_BRANCHES,
) -> str:
    """List branches, or switch to (optionally create) one."""
    root = _repo_root(path)

    if name:
        target = str(name).strip()
        args = ["switch", "-c", target] if create else ["switch", target]
        code, out, err = _git(args, root)
        if code != 0:
            raise GitError(err or out or f"could not switch to {target}")
        # git switch reports success on stderr.
        return f"Repo: {root}\n{err or out or f'Switched to {target}'}"

    code, out, err = _git(
        [
            "branch",
            "--list",
            "--sort=-committerdate",
            "--format=%(HEAD) %(refname:short) | %(upstream:short) | %(committerdate:relative)",
        ],
        root,
    )
    if code != 0:
        raise GitError(err or "git branch failed")

    rows = [row.strip() for row in out.splitlines() if row.strip()]
    report = [f"Repo: {root}", f"Branches ({len(rows)}), current marked with *:"]
    report.extend(f"  {row}" for row in rows[:limit])
    if len(rows) > limit:
        report.append(f"  ... and {len(rows) - limit} more")
    return "\n".join(report)


def _remotes(root: str) -> list[str]:
    code, out, _ = _git(["remote", "-v"], root)
    if code != 0 or not out.strip():
        return ["(none)"]
    seen: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen.setdefault(parts[0], parts[1])
    return [f"{key}: {value}" for key, value in seen.items()]


def _github_status() -> list[str]:
    """Is the GitHub CLI installed, and is an account actually logged in?"""
    try:
        _, version, _ = _run_exe("gh", ["--version"], timeout=10)
    except GitError as exc:
        return [f"GitHub CLI: {exc}"]

    head = version.splitlines()[0] if version else "installed"
    lines = [f"GitHub CLI: {head}"]
    try:
        code, out, err = _run_exe("gh", ["auth", "status"], timeout=15)
    except GitError as exc:
        lines.append(f"  auth: {exc}")
        return lines

    detail = (out or err).strip()
    if not detail:
        lines.append("  auth: not logged in" if code != 0 else "  auth: no details reported")
        return lines
    for line in detail.splitlines()[:GH_STATUS_LINES]:
        text = line.strip()
        if text:
            lines.append(f"  {text}")
    return lines


def _discover_repos(
    start: str,
    *,
    depth: int = DEFAULT_SCAN_DEPTH,
    cap: int = DEFAULT_SCAN_CAP,
    skip: str | None = None,
) -> list[str]:
    """Directories holding a .git entry within *depth* levels of *start*."""
    origin = os.path.abspath(start or ".")
    found: list[str] = []
    stack: list[tuple[str, int]] = [(origin, 0)]

    while stack and len(found) < cap:
        current, level = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue

        if any(entry.name == ".git" for entry in entries):
            if skip is None or os.path.normpath(current) != os.path.normpath(skip):
                found.append(current)
            continue  # never descend into a repository

        if level >= depth:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            stack.append((entry.path, level + 1))

    return sorted(found)


@_reports_errors
def info(path: str = ".", *, depth: int = DEFAULT_SCAN_DEPTH, limit: int = DEFAULT_SCAN_CAP) -> str:
    """Git version, identity, this repo, GitHub CLI auth and nearby repositories."""
    cwd = _resolve_cwd(path)
    report: list[str] = []

    code, version, _ = _run_exe("git", ["--version"])
    report.append(version if code == 0 and version else "git: not available")

    name = _config_value("user.name", cwd)
    email = _config_value("user.email", cwd)
    if name and email:
        report.append(f"Identity: {name} <{email}>")
    else:
        report.append(
            "Identity: not configured - mode=commit reuses the last commit's author, "
            f"or {FALLBACK_NAME} <{FALLBACK_EMAIL}>"
        )

    try:
        root = _repo_root(cwd)
    except GitError:
        root = None

    report.append("")
    if root:
        _, branch_name, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
        _, dirty, _ = _git(["status", "--porcelain"], root)
        changed = len([line for line in dirty.splitlines() if line.strip()])
        report.append(f"Current repo: {root}")
        report.append(f"  branch: {branch_name or '(unknown)'}")
        report.append(f"  last commit: {_last_commit(root)}")
        report.append(f"  working tree: {'clean' if changed == 0 else f'{changed} changed file(s)'}")
        for line in _remotes(root):
            report.append(f"  remote {line}")
    else:
        report.append(f"Current directory is not a git repository: {cwd}")

    report.append("")
    report.extend(_github_status())

    others = _discover_repos(
        os.path.dirname(root) if root else cwd, depth=depth, cap=limit, skip=root
    )
    report.append("")
    if others:
        report.append(f"Other repositories nearby ({len(others)}):")
        report.extend(f"  {item}" for item in others)
    else:
        report.append("No other repositories found nearby.")
    return "\n".join(report)
