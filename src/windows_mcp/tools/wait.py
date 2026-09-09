"""Wait tool — pause the agent for a fixed number of seconds.

This is the one survivor of the old GUI input tool set (Click / Type / Scroll /
Move / Shortcut / WaitFor were dropped in Windows-MCP Pro): an explicit sleep is
still the cheapest way to let a service finish starting, a file finish being
written, or a rate limit expire before the next command runs.

The wait is capped because the MCP client aborts a call at roughly 60 seconds -
a longer sleep would throw away whatever the agent was waiting for. Use the Job
tool for anything that needs to outlive a single tool call.
"""

import time

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context

# The MCP client gives up on a tool call at ~60s. Sleeping longer than this
# guarantees the caller never sees the result, so the request is clamped and the
# clamp is reported back instead of being silently ignored.
MAX_WAIT_SECONDS = 45

_DESCRIPTION = (
    "Pause for a number of seconds. Keywords: wait, sleep, pause, delay, retry later. "
    "Use it to let a service finish starting, a build settle, a file appear, or a rate "
    f"limit expire before the next call. Capped at {MAX_WAIT_SECONDS} seconds because the "
    "MCP client aborts a call at ~60s; for longer waiting start the work with the 'Job' "
    "tool and poll 'Job mode=status' instead."
)


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Wait",
        description=_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Wait",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Wait-Tool")
    def wait_tool(duration: int, ctx: Context = None) -> str:
        if isinstance(duration, bool) or not isinstance(duration, (int, float, str)):
            raise ValueError(f"duration must be a number of seconds, got {duration!r}")
        try:
            requested = int(float(duration))
        except (TypeError, ValueError):
            raise ValueError(f"duration must be a number of seconds, got {duration!r}")
        if requested < 0:
            raise ValueError(f"duration must not be negative, got {requested}")

        seconds = min(requested, MAX_WAIT_SECONDS)
        time.sleep(seconds)

        if seconds != requested:
            return (
                f"Waited for {seconds} seconds (requested {requested}). "
                f"Waits are capped at {MAX_WAIT_SECONDS}s because the MCP client aborts the "
                "call at ~60s and the result would be lost. For longer work use "
                "Job mode=start command=... and poll Job mode=status."
            )
        return f"Waited for {seconds} seconds."

    return wait_tool
