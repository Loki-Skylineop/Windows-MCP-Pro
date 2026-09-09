"""PowerShell tool — shell/command execution."""

from mcp.types import ToolAnnotations
from windows_mcp.coding import shell_service
from windows_mcp.infrastructure import with_analytics
from windows_mcp.powershell import PowerShellExecutor
from fastmcp import Context

_DESCRIPTION = (
    "Shell/command execution. Keywords: shell, run, execute, cmd, terminal, command line, script. "
    "A comprehensive system tool for executing any PowerShell commands. Use it to navigate the "
    "file system, manage files and processes, and execute system-level operations. Capable of "
    "accessing web content (e.g., via Invoke-WebRequest), interacting with network resources, and "
    "performing complex administrative tasks. This tool provides full access to the underlying "
    "operating system capabilities, making it the primary interface for system automation, "
    "scripting, and deep system interaction.\n\n"
    "Coding-friendly behaviour:\n"
    "  cwd      run the command in this directory instead of the home directory\n"
    "  session  a name that persists the working directory and environment variables between "
    "calls, so 'cd', '$env:...' and activated virtualenvs survive (e.g. session='build')\n"
    "  timeout  seconds, up to 3600; the command's own exit code is reported, stdout and stderr "
    "are both returned, and if the timeout is hit the output captured so far is preserved instead "
    "of being discarded\n\n"
    "For anything genuinely long-running (installs, builds, test suites, watchers) use the 'Job' "
    "tool, which runs detached and streams to a log file. For editing files use 'Edit', and for "
    "searching file contents use 'Grep' — both are far cheaper than shelling out."
)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="PowerShell",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="PowerShell",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "Powershell-Tool")
    def powershell_tool(
        command: str,
        timeout: int = 30,
        cwd: str = None,
        session: str = None,
        ctx: Context = None,
    ) -> str:
        try:
            result = shell_service.run(command, timeout=timeout, cwd=cwd, session=session)
            return shell_service.format_result(result)
        except Exception:
            # Never lose the ability to run a command: fall back to the plain executor.
            response, status_code = PowerShellExecutor.execute_command(command, timeout)
            return f"Response: {response}\nStatus Code: {status_code}"
