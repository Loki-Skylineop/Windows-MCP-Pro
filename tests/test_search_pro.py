"""Tests for the SearchPro tool: service layer + out-of-process worker.

The old `Scrape` tool had no tests at all, which is how it shipped a default
path (`use_sampling=True`) that silently degraded to raw HTML on every client
that does not implement MCP sampling. Everything worth getting wrong here -
argument validation, timeout clamping, interpreter discovery, output budgeting,
block-page detection and the stdout protocol - is exercised without touching the
network.
"""

from __future__ import annotations

import json

import pytest

from windows_mcp.websearch import search_service
from windows_mcp.websearch import worker


class _WorkerRecorder:
    """Stands in for the subprocess boundary and records what crossed it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reply: dict = {"ok": True}

    def set_reply(self, new_reply: dict) -> None:
        self.reply = dict(new_reply)

    def last_payload(self) -> dict:
        return self.calls[-1]["payload"]


@pytest.fixture
def stub_worker(monkeypatch):
    recorder = _WorkerRecorder()

    def fake_run(payload, timeout):
        recorder.calls.append({"payload": payload, "timeout": timeout})
        return dict(recorder.reply)

    monkeypatch.setattr(search_service, "_run_worker", fake_run)
    # The SSRF guard resolves hostnames; these tests must never need DNS.
    monkeypatch.setattr(search_service, "_validate_target", lambda url: None)
    return recorder


@pytest.fixture
def guarded_worker(monkeypatch):
    """Same subprocess stub, but the real SSRF guard stays in place."""
    recorder = _WorkerRecorder()

    def fake_run(payload, timeout):
        recorder.calls.append({"payload": payload, "timeout": timeout})
        return dict(recorder.reply)

    monkeypatch.setattr(search_service, "_run_worker", fake_run)
    return recorder


class TestArgumentValidation:
    def test_unknown_mode_is_rejected(self, stub_worker):
        with pytest.raises(ValueError, match="mode must be one of"):
            search_service.run(mode="telepathy", query="x")

    def test_search_requires_a_query(self, stub_worker):
        with pytest.raises(ValueError, match="requires query"):
            search_service.run(mode="search")

    @pytest.mark.parametrize("mode", ["read", "crawl", "select"])
    def test_url_modes_require_a_url(self, mode, stub_worker):
        with pytest.raises(ValueError, match="requires url"):
            search_service.run(mode=mode)

    def test_select_requires_selectors(self, stub_worker):
        with pytest.raises(ValueError, match="requires selectors"):
            search_service.run(mode="select", url="https://example.com")

    def test_bad_timelimit_is_rejected(self, stub_worker):
        with pytest.raises(ValueError, match="timelimit must be one of"):
            search_service.run(mode="news", query="x", timelimit="decade")

    def test_mode_is_case_insensitive(self, stub_worker):
        stub_worker.set_reply({"ok": True, "results": [], "engine": "ddgs:auto"})
        search_service.run(mode="SEARCH", query="x")
        assert stub_worker.last_payload()["mode"] == "search"


class TestSelectorParsing:
    def test_shorthand_pairs(self):
        parsed = search_service.parse_selectors(
            "title=span.titleline > a::text; url=span.titleline > a::attr(href)"
        )
        assert parsed == {
            "title": "span.titleline > a::text",
            "url": "span.titleline > a::attr(href)",
        }

    def test_json_object(self):
        assert search_service.parse_selectors('{"a": ".x"}') == {"a": ".x"}

    def test_dict_passes_through(self):
        assert search_service.parse_selectors({"a": ".x"}) == {"a": ".x"}

    def test_pair_without_equals_is_rejected(self):
        with pytest.raises(ValueError, match="name=css-selector"):
            search_service.parse_selectors("div.result")


class TestTimeoutClamp:
    def test_long_timeout_is_clamped_and_reported(self, stub_worker, monkeypatch):
        monkeypatch.delenv(search_service.ENV_CLIENT_TIMEOUT, raising=False)
        stub_worker.set_reply({"ok": True, "results": [], "engine": "ddgs:auto"})

        out = search_service.run(mode="search", query="x", timeout=600)

        assert stub_worker.calls[-1]["timeout"] == search_service.CLIENT_TIMEOUT_CEILING
        assert "clamped" in out

    def test_env_can_disable_the_ceiling(self, stub_worker, monkeypatch):
        monkeypatch.setenv(search_service.ENV_CLIENT_TIMEOUT, "0")
        stub_worker.set_reply({"ok": True, "results": [], "engine": "ddgs:auto"})

        out = search_service.run(mode="search", query="x", timeout=600)

        assert stub_worker.calls[-1]["timeout"] == 600
        assert "clamped" not in out

    def test_zero_timeout_is_rejected(self, stub_worker):
        with pytest.raises(ValueError, match="timeout must be positive"):
            search_service.run(mode="search", query="x", timeout=-5)


class TestOutputBudget:
    def test_results_are_clamped(self, stub_worker):
        stub_worker.set_reply({"ok": True, "results": [], "engine": "ddgs:auto"})

        out = search_service.run(mode="search", query="x", max_results=999)

        assert stub_worker.last_payload()["max_results"] == search_service.HARD_MAX_RESULTS
        assert f"clamped to {search_service.HARD_MAX_RESULTS}" in out

    def test_page_text_is_trimmed_with_a_hint(self, stub_worker):
        stub_worker.set_reply(
            {"ok": True, "engine": "trafilatura", "url": "u", "text": "x" * 500, "chars": 500}
        )

        out = search_service.run(mode="read", url="https://example.com", max_chars=200)

        assert "showing 200 of 500 characters" in out
        assert "Raise max_chars" in out

    def test_short_text_is_not_trimmed(self, stub_worker):
        stub_worker.set_reply({"ok": True, "engine": "trafilatura", "url": "u", "text": "hello"})

        out = search_service.run(mode="read", url="https://example.com")

        assert "hello" in out
        assert "trimmed" not in out


class TestFormatting:
    def test_search_results_carry_title_url_and_snippet(self, stub_worker):
        stub_worker.set_reply(
            {
                "ok": True,
                "engine": "ddgs:yandex",
                "query": "crawl4ai",
                "region": "ru-ru",
                "elapsed": 0.6,
                "results": [
                    {
                        "title": "Crawl4AI",
                        "url": "https://github.com/unclecode/crawl4ai",
                        "snippet": "Open-source crawler",
                        "extra": {"date": "2026-09-01"},
                    }
                ],
            }
        )

        out = search_service.run(mode="search", query="crawl4ai", region="ru-ru")

        assert "engine=ddgs:yandex" in out
        assert "https://github.com/unclecode/crawl4ai" in out
        assert "Open-source crawler" in out
        assert "date=2026-09-01" in out

    def test_block_page_is_flagged_not_reported_as_content(self, stub_worker):
        stub_worker.set_reply(
            {
                "ok": True,
                "engine": "urllib+striptags",
                "url": "https://blocked.example",
                "text": "Just a moment... please enable javascript",
                "blocked": True,
            }
        )

        out = search_service.run(mode="read", url="https://blocked.example")

        assert "WARNING" in out
        assert "captcha" in out.lower()

    def test_empty_results_say_so(self, stub_worker):
        stub_worker.set_reply(
            {"ok": True, "engine": "ddgs:auto", "results": [], "tried": ["brave: 0 results"]}
        )

        out = search_service.run(mode="search", query="x")

        assert "(no results)" in out
        assert "brave: 0 results" in out

    def test_worker_failure_raises_instead_of_returning_error_text(self, stub_worker):
        stub_worker.set_reply({"ok": False, "error": "boom", "tried": ["yandex: dead"]})

        with pytest.raises(RuntimeError) as excinfo:
            search_service.run(mode="search", query="x")

        assert "boom" in str(excinfo.value)
        assert "yandex: dead" in str(excinfo.value)


class TestStdoutProtocol:
    def test_reply_survives_library_noise(self):
        noisy = "\n".join(
            [
                "INFO scrapling: fetching",
                "[crawl4ai] progress 42%",
                search_service.SENTINEL + json.dumps({"ok": True, "mode": "search"}),
                "INFO scrapling: done",
            ]
        )

        assert search_service._extract_reply(noisy) == {"ok": True, "mode": "search"}

    def test_missing_sentinel_returns_none(self):
        assert search_service._extract_reply("nothing to see here") is None

    def test_broken_json_falls_back_to_an_earlier_line(self):
        text = "\n".join(
            [
                search_service.SENTINEL + json.dumps({"ok": True, "mode": "read"}),
                search_service.SENTINEL + "{not json",
            ]
        )

        assert search_service._extract_reply(text) == {"ok": True, "mode": "read"}


class TestInterpreterDiscovery:
    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch):
        monkeypatch.setattr(search_service, "_interpreter_cache", None)

    def test_first_candidate_with_the_stack_wins(self, monkeypatch):
        monkeypatch.setattr(search_service, "_candidates", lambda: ["a.exe", "b.exe"])
        monkeypatch.setattr(search_service.os.path, "isfile", lambda path: True)
        monkeypatch.setattr(search_service, "_has_module", lambda path, module="ddgs": path == "b.exe")

        assert search_service.resolve_interpreter(force=True) == "b.exe"
        assert "a.exe: no ddgs" in search_service.discovery_log()

    def test_missing_stack_explains_how_to_install(self, monkeypatch):
        monkeypatch.setattr(search_service, "_candidates", lambda: ["a.exe"])
        monkeypatch.setattr(search_service.os.path, "isfile", lambda path: True)
        monkeypatch.setattr(search_service, "_has_module", lambda path, module="ddgs": False)

        with pytest.raises(search_service.SearchStackMissing) as excinfo:
            search_service.resolve_interpreter(force=True)

        message = str(excinfo.value)
        assert search_service.ENV_INTERPRETER in message
        assert "pip install ddgs" in message

    def test_env_override_is_probed_and_reported(self, monkeypatch):
        monkeypatch.setenv(search_service.ENV_INTERPRETER, "C:/custom/python.exe")
        monkeypatch.setattr(search_service.os.path, "isfile", lambda path: True)
        monkeypatch.setattr(search_service, "_has_module", lambda path, module="ddgs": False)

        with pytest.raises(search_service.SearchStackMissing, match="C:/custom/python.exe"):
            search_service.resolve_interpreter(force=True)

    def test_child_env_forces_utf8_and_drops_pythonpath(self, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "C:/server/src")

        env = search_service._child_env()

        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
        assert "PYTHONPATH" not in env


class TestWorkerHelpers:
    def test_block_markers_only_apply_to_short_documents(self):
        assert worker._looks_blocked("Please enable JavaScript to continue") is True
        long_article = "captcha " + ("real content " * 500)
        assert worker._looks_blocked(long_article) is False
        assert worker._looks_blocked("") is False

    def test_rows_are_normalised_across_ddgs_shapes(self):
        rows = worker._normalise_rows(
            [
                {"title": "A", "href": "https://a", "body": "first  result"},
                {"title": "B", "url": "https://b", "description": "second", "source": "RIA"},
            ]
        )

        assert rows[0] == {
            "title": "A",
            "url": "https://a",
            "snippet": "first result",
            "extra": {},
        }
        assert rows[1]["extra"] == {"source": "RIA"}

    def test_duplicate_urls_are_dropped(self):
        rows = [{"url": "https://a"}, {"url": "https://a"}, {"url": "https://b"}]
        assert worker._dedupe(rows) == [{"url": "https://a"}, {"url": "https://b"}]

    def test_html_fallback_strips_scripts_and_tags(self):
        html = "<html><script>evil()</script><p>Hello</p><style>x{}</style></html>"
        assert "evil" not in worker._html_to_text(html)
        assert "Hello" in worker._html_to_text(html)

    def test_ddgs_kwargs_are_dropped_until_the_call_fits(self):
        seen: list[dict] = []

        def picky(query, **kwargs):
            seen.append(dict(kwargs))
            if "timelimit" in kwargs:
                raise TypeError("unexpected keyword argument 'timelimit'")
            return [{"title": query}]

        rows = worker._call_ddgs(picky, "q", max_results=3, timelimit="d")

        assert rows == [{"title": "q"}]
        assert "timelimit" in seen[0] and "timelimit" not in seen[1]


class TestWorkerEntryPoint:
    def test_unknown_mode_exits_with_a_readable_reply(self, tmp_path, capsys):
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"mode": "nope"}), encoding="utf-8")

        code = worker.main(["worker.py", str(payload)])
        captured = capsys.readouterr().out

        assert code == 2
        reply = json.loads(captured.split(worker.SENTINEL, 1)[1])
        assert reply["ok"] is False
        assert "unknown mode" in reply["error"]

    def test_env_mode_reports_versions_offline(self, tmp_path, capsys):
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"mode": "env", "probe": False}), encoding="utf-8")

        code = worker.main(["worker.py", str(payload)])
        reply = json.loads(capsys.readouterr().out.split(worker.SENTINEL, 1)[1])

        assert code == 0
        assert reply["ok"] is True
        assert "ddgs" in reply["versions"]
        assert reply["probe"] == {}

    def test_missing_payload_path_is_reported(self, capsys):
        code = worker.main(["worker.py"])
        reply = json.loads(capsys.readouterr().out.split(worker.SENTINEL, 1)[1])

        assert code == 2
        assert "payload file path" in reply["error"]

    def test_payload_with_a_utf8_bom_is_accepted(self, tmp_path, capsys):
        """PowerShell writes UTF8 files with a BOM; json.load alone rejects them."""
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"mode": "env", "probe": False}), encoding="utf-8-sig")

        code = worker.main(["worker.py", str(payload)])
        reply = json.loads(capsys.readouterr().out.split(worker.SENTINEL, 1)[1])

        assert code == 0
        assert reply["ok"] is True


class TestSsrfGuard:
    """SearchPro must not become an SSRF hole now that Scrape is gone.

    Upstream ran every scraped URL through ``validate_url``. Replacing the tool
    without that check would let a prompt-injected agent read
    ``http://127.0.0.1`` or a cloud metadata endpoint through the server's own
    network position. Literal IPs are used throughout so these tests stay
    offline.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/health",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
        ],
    )
    def test_private_targets_are_refused(self, url, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", raising=False)
        with pytest.raises(ValueError, match="refusing to fetch"):
            search_service._validate_target(url)

    def test_non_http_schemes_are_refused(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", raising=False)
        with pytest.raises(ValueError, match="not allowed"):
            search_service._validate_target("file:///C:/Users/me/.ssh/id_rsa")

    def test_embedded_credentials_are_refused(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", raising=False)
        with pytest.raises(ValueError, match="credentials"):
            search_service._validate_target("https://user:secret@example.com/")

    def test_the_env_flag_allows_a_local_dev_server(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", "1")
        search_service._validate_target("http://127.0.0.1:8000/health")

    def test_run_refuses_before_spawning_the_worker(self, guarded_worker, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", raising=False)
        with pytest.raises(ValueError, match="refusing to fetch"):
            search_service.run(mode="read", url="http://169.254.169.254/latest/meta-data/")
        assert guarded_worker.calls == []

    @pytest.mark.parametrize("mode", ["read", "select", "crawl"])
    def test_every_url_mode_is_guarded(self, mode, guarded_worker, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_SEARCH_ALLOW_PRIVATE", raising=False)
        with pytest.raises(ValueError, match="refusing to fetch"):
            search_service.run(
                mode=mode,
                url="http://127.0.0.1:9/x",
                selectors="title=h1::text",
            )
        assert guarded_worker.calls == []

    def test_search_mode_needs_no_url_check(self, guarded_worker):
        guarded_worker.set_reply({"ok": True, "results": [], "engine": "ddgs:auto"})
        search_service.run(mode="search", query="windows mcp")
        assert len(guarded_worker.calls) == 1
