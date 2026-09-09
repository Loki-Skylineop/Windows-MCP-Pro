"""Git tool - repository status, diffs, history, commits, branches and setup."""

import os
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations
from platformdirs import user_desktop_dir

from windows_mcp.coding import git_service
from windows_mcp.infrastructure import with_analytics

_DESCRIPTION = (
    "Git for a local repository. Keywords: git status, what changed, uncommitted, diff, "
    "staged, history, log, recent commits, commit my changes, branches, switch branch, "
    "which repositories do I have, is GitHub connected, remotes.\n\n"
    "mode='status' (default): branch and upstream, last commit, and the changed files "
    "grouped into staged / unstaged / untracked.\n"
    "mode='diff': a --stat summary plus the patch, trimmed to 'max_lines' (default 200). "
    "staged=true diffs the index, target='HEAD~3' or a branch name compares against it, "
    "file=... narrows to one path.\n"
    "mode='log': recent commits, one line each. limit (default 15), pattern=... greps "
    "commit messages, file=... limits history to one path.\n"
    "mode='commit': stages everything (add_all=true, the default) or just 'paths', then "
    "commits 'message'. If no git identity is configured it reuses the previous commit's "
    "author instead of failing with 'Author identity unknown'. It never pushes.\n"
    "mode='branch': list branches with upstream and age; name=... switches to one, "
    "name=... with create=true starts a new one.\n"
    "mode='info': git version, configured identity, the current repository with its "
    "remotes, whether the GitHub CLI is installed and logged in, and other repositories "
    "found next to this one.\n\n"
    "push, reset, rebase and history rewriting are deliberately NOT here - run those "
    "through PowerShell, where the exact command is visible before it executes."
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
        name="Git",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Git",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Git-Tool")
    def git_tool(
        mode: Literal["status", "diff", "log", "commit", "branch", "info"] = "status",
        path: str = ".",
        message: str = None,
        target: str = None,
        file: str = None,
        pattern: str = None,
        name: str = None,
        paths: str = None,
        staged: Any = False,
        create: Any = False,
        add_all: Any = True,
        limit: int = None,
        max_lines: int = None,
        ctx: Context = None,
    ) -> str:
        action = str(mode or "status").strip().lower()
        root = _resolve(path)

        if action == "status":
            return git_service.status(
                root, limit=int(limit or git_service.DEFAULT_STATUS_FILES)
            )
        if action == "diff":
            return git_service.diff(
                root,
                staged=_truthy(staged),
                target=target,
                file=file,
                max_lines=int(max_lines or git_service.DEFAULT_DIFF_LINES),
            )
        if action == "log":
            return git_service.log(
                root,
                limit=int(limit or git_service.DEFAULT_LOG_LIMIT),
                file=file,
                pattern=pattern,
                target=target,
            )
        if action == "commit":
            return git_service.commit(
                root, message=message, add_all=_truthy(add_all, True), paths=paths
            )
        if action == "branch":
            return git_service.branch(
                root,
                name=name,
                create=_truthy(create),
                limit=int(limit or git_service.DEFAULT_BRANCHES),
            )
        if action == "info":
            return git_service.info(root, limit=int(limit or git_service.DEFAULT_SCAN_CAP))

        return "Error: mode must be 'status', 'diff', 'log', 'commit', 'branch' or 'info'."

    return git_tool
