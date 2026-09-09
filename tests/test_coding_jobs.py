"""Tests for windows_mcp.coding.jobs_service (detached background jobs).

These spawn real PowerShell processes, so they are kept short and each test
gets its own jobs root inside tmp_path.
"""

import time

import pytest

from windows_mcp.coding import jobs_service


@pytest.fixture()
def jobs_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_service, "JOBS_ROOT", str(root))
    return root


def job_id_of(started_output: str) -> str:
    assert started_output.startswith("Started job "), started_output
    return started_output.split("Started job ", 1)[1].split(" ", 1)[0].strip()


def wait_for_finish(job_id: str, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    state = jobs_service.status(job_id)
    while time.time() < deadline:
        if "[succeeded" in state or "[failed" in state or "[lost" in state:
            return state
        time.sleep(0.3)
        state = jobs_service.status(job_id)
    return state


class TestEmptyRoot:
    def test_status_without_jobs(self, jobs_root):
        assert jobs_service.status().startswith("No jobs found")

    def test_list_without_jobs(self, jobs_root):
        assert jobs_service.list_jobs().startswith("No jobs found")

    def test_logs_without_jobs(self, jobs_root):
        assert jobs_service.logs().startswith("No jobs found")

    def test_stop_without_jobs(self, jobs_root):
        assert jobs_service.stop().startswith("No jobs found")


class TestValidation:
    def test_empty_command(self, jobs_root):
        assert jobs_service.start("").startswith("Error:")

    def test_missing_cwd(self, jobs_root):
        assert jobs_service.start("echo x", cwd=r"C:\definitely\not\here").startswith("Error:")


class TestLifecycle:
    def test_success_path(self, jobs_root, tmp_path):
        started = jobs_service.start("Write-Output 'hello-job'", cwd=str(tmp_path), name="hello")
        job_id = job_id_of(started)
        assert "Log: " in started

        state = wait_for_finish(job_id)
        assert "[succeeded exit=0]" in state

        out = jobs_service.logs(job_id, tail=20)
        assert "hello-job" in out
        assert "job finished with exit code 0" in out

    def test_failure_exit_code(self, jobs_root, tmp_path):
        job_id = job_id_of(jobs_service.start("cmd /c exit 5", cwd=str(tmp_path), name="boom"))
        assert "[failed exit=5]" in wait_for_finish(job_id)

    def test_unicode_output(self, jobs_root, tmp_path):
        job_id = job_id_of(jobs_service.start(
            "Write-Output '\u041f\u0440\u0438\u0432\u0435\u0442 \u2014 \U0001f680'",
            cwd=str(tmp_path),
        ))
        wait_for_finish(job_id)
        out = jobs_service.logs(job_id, tail=10)
        assert "\u041f\u0440\u0438\u0432\u0435\u0442" in out
        assert "\U0001f680" in out

    def test_streaming_and_stop(self, jobs_root, tmp_path):
        job_id = job_id_of(jobs_service.start(
            "1..40 | ForEach-Object { Write-Output \"tick-$_\"; Start-Sleep -Milliseconds 300 }",
            cwd=str(tmp_path),
            name="ticker",
        ))
        deadline = time.time() + 15
        streaming = ""
        while time.time() < deadline:
            streaming = jobs_service.logs(job_id, tail=20)
            if "tick-1" in streaming:
                break
            time.sleep(0.3)
        assert "tick-1" in streaming, streaming
        assert "[running]" in jobs_service.status(job_id)

        assert jobs_service.stop(job_id).startswith("Stopped job ")
        time.sleep(1.0)
        assert "[running]" not in jobs_service.status(job_id)
        assert "is not running" in jobs_service.stop(job_id)

    def test_log_views(self, jobs_root, tmp_path):
        job_id = job_id_of(jobs_service.start(
            "1..5 | ForEach-Object { Write-Output \"row-$_\" }", cwd=str(tmp_path)
        ))
        wait_for_finish(job_id)
        assert "first 2 of" in jobs_service.logs(job_id, head=2)
        assert "last 3 of" in jobs_service.logs(job_id, tail=3)
        grepped = jobs_service.logs(job_id, pattern="row-4")
        assert "row-4" in grepped and "grep" in grepped
        assert jobs_service.logs(job_id, pattern="[bad").startswith("Error:")


class TestResolution:
    def test_aliases_and_listing(self, jobs_root, tmp_path):
        job_id = job_id_of(jobs_service.start("Write-Output 'x'", cwd=str(tmp_path), name="alias"))
        wait_for_finish(job_id)
        assert job_id in jobs_service.status("last")
        assert job_id in jobs_service.status(job_id[:14])
        assert jobs_service.status("definitely-not-a-job").startswith("No jobs found")

        listing = jobs_service.list_jobs(limit=5)
        assert "job(s) total" in listing
        assert job_id in listing

    def test_clean_prunes_old_jobs(self, jobs_root, tmp_path):
        first = job_id_of(jobs_service.start("Write-Output 'one'", cwd=str(tmp_path)))
        wait_for_finish(first)
        second = job_id_of(jobs_service.start("Write-Output 'two'", cwd=str(tmp_path)))
        wait_for_finish(second)

        report = jobs_service.clean(keep=1)
        assert not report.startswith("Error:")
        remaining = jobs_service.list_jobs()
        assert second in remaining

    def test_ordering_uses_start_time_not_random_suffix(self, jobs_root):
        """Two jobs from the same second must order by clock, not by hex suffix."""
        import json
        import os

        older = "20990101-120000-alpha-zzzzzz"
        newer = "20990101-120000-alpha-aaaaaa"
        for name, stamp in ((older, 1000.0), (newer, 2000.0)):
            folder = os.path.join(jobs_service.JOBS_ROOT, name)
            os.makedirs(folder, exist_ok=True)
            meta = {"command": "noop", "cwd": ".", "started_monotonic": stamp}
            with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle)

        assert jobs_service._all_job_ids() == [older, newer]
        assert jobs_service._resolve_job("last") == newer
        assert jobs_service._resolve_job("20990101-120000") == newer
