"""Job tool - detached background commands for builds, installs and test runs."""

import os
from typing import Literal

from fastmcp import Context
from mcp.types import ToolAnnotations
from platformdirs import user_desktop_dir

from windows_mcp.coding import jobs_service
from windows_mcp.infrastructure import with_analytics

_DESCRIPTION = (
    "Run long commands in the background. Keywords: background job, async command, build, "
    "npm install, pip install, pytest, test run, compile, watch, long running, timeout. "
    "The PowerShell tool is synchronous, so anything slower than its timeout gets killed - "
    "start it here instead and poll for output.\n\n"
    "mode='start'  : spawn 'command' detached (optionally in 'cwd', with a friendly 'name'), "
    "returns a job id immediately. Output is streamed to a UTF-8 log file line by line.\n"
    "mode='status' : state (running / succeeded / failed), real exit code, pid, runtime, log size "
    "and the last output line. Defaults to the most recent job.\n"
    "mode='logs'   : tail=N last lines (default 80), head=N first lines, or pattern=REGEX to grep "
    "the log while the job is still running.\n"
    "mode='stop'   : kill the job and its whole process tree.\n"
    "mode='list'   : recent jobs with their state.\n"
    "mode='clean'  : delete all but the newest 'keep' finished job folders.\n\n"
    "job_id accepts a full id, a unique prefix, or 'last'. Jobs survive server restarts because "
    "each one keeps its command, log and exit code in its own folder."
)


def _resolve(path: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(str(path)))
    if not os.path.isabs(expanded):
        expanded = os.path.join(user_desktop_dir(), expanded)
    return os.path.abspath(expanded)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Job",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Job",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Job-Tool")
    def job_tool(
        mode: Literal["start", "status", "logs", "stop", "list", "clean"] = "status",
        command: str = None,
        cwd: str = None,
        job_id: str = None,
        name: str = None,
        tail: int = 80,
        head: int = None,
        pattern: str = None,
        keep: int = 10,
        limit: int = 15,
        ctx: Context = None,
    ) -> str:
        action = str(mode or "status").strip().lower()

        if action == "start":
            if not command:
                return "Error: 'command' is required for mode='start'."
            return jobs_service.start(
                command,
                cwd=_resolve(cwd) if cwd else None,
                name=name,
            )
        if action == "status":
            return jobs_service.status(job_id)
        if action == "logs":
            return jobs_service.logs(
                job_id,
                tail=int(tail or 80),
                head=None if head in (None, 0) else int(head),
                pattern=pattern,
            )
        if action == "stop":
            return jobs_service.stop(job_id)
        if action == "list":
            return jobs_service.list_jobs(limit=int(limit or 15))
        if action == "clean":
            return jobs_service.clean(keep=int(keep or 0))
        return "Error: mode must be start, status, logs, stop, list or clean."
