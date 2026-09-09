"""Coding layer - services that make this server usable for real software work.

* :mod:`edit_service`  surgical, validated, atomic multi-file edits
* :mod:`grep_service`  content search, repository maps and symbol outlines
* :mod:`jobs_service`  detached background jobs for builds, installs and tests
* :mod:`shell_service` PowerShell with cwd, sessions, real exit codes and
  partial output on timeout

They are deliberately free of MCP plumbing so they can be unit-tested directly;
the thin tool wrappers live in :mod:`windows_mcp.tools`.
"""

from windows_mcp.coding import edit_service, grep_service, jobs_service, shell_service

__all__ = ["edit_service", "grep_service", "jobs_service", "shell_service"]
