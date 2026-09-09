"""Cross-cutting behaviour applied to every tool at registration time.

Two problems used to be invisible in the happy path:

* clients saw a flat list of tool names with no grouping;
* a service returning ``Error: file not found`` was reported to the client as a
  successful call, so no client retried it and no UI flagged it.

Both are fixed centrally in ``windows_mcp.tools``, which is why they are tested
here: every future tool module inherits the behaviour, and these tests fail if
the wiring is removed.
"""

from __future__ import annotations

import inspect

import pytest
from fastmcp.exceptions import ToolError

from windows_mcp import tools
from windows_mcp.tools import (
    CATEGORIES,
    CategorisedMCP,
    categories,
    looks_like_failure,
    surface_errors,
    tag_description,
)


class _FakeMCP:
    """Records what a tool module registers, the way FastMCP would receive it."""

    def __init__(self) -> None:
        self.registered: list[dict] = []

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.registered.append({"args": args, "kwargs": kwargs, "fn": fn})
            return fn

        return decorate

    def ping(self) -> str:
        """Stands in for any other FastMCP API a tool module might reach for."""
        return "pong"


class TestCategoryTags:
    def test_the_tag_is_prepended(self) -> None:
        assert tag_description("coding", "Edits files.") == "[coding] Edits files."

    def test_tagging_is_idempotent(self) -> None:
        once = tag_description("web", "Searches the web.")
        assert tag_description("web", once) == once

    def test_a_missing_description_stays_missing(self) -> None:
        assert tag_description("util", None) is None

    def test_descriptions_are_tagged_through_the_proxy(self) -> None:
        fake = _FakeMCP()
        proxy = CategorisedMCP(fake, "coding")

        @proxy.tool(name="Demo", description="Does a thing.")
        def demo() -> str:
            return "ok"

        assert fake.registered[0]["kwargs"]["description"] == "[coding] Does a thing."

    def test_unknown_attributes_reach_the_real_server(self) -> None:
        proxy = CategorisedMCP(_FakeMCP(), "web")
        assert proxy.ping() == "pong"
        assert proxy.category == "web"

    def test_every_module_has_a_known_category(self) -> None:
        mapping = categories()
        assert len(mapping) == len(tools._MODULES)
        assert set(mapping.values()) <= set(CATEGORIES)

    def test_the_expected_modules_are_grouped_as_documented(self) -> None:
        mapping = categories()
        assert mapping["edit"] == "coding"
        assert mapping["grep"] == "coding"
        assert mapping["git"] == "coding"
        assert mapping["search"] == "web"
        assert mapping["registry"] == "windows"
        assert mapping["wait"] == "util"


class TestErrorSurfacing:
    def test_an_error_string_becomes_a_tool_error(self) -> None:
        wrapped = surface_errors(lambda: "Error: file not found: nope.txt")
        with pytest.raises(ToolError, match="file not found"):
            wrapped()

    def test_a_value_error_becomes_a_tool_error(self) -> None:
        def boom() -> str:
            raise ValueError("mode must be one of search, read, crawl")

        with pytest.raises(ToolError, match="mode must be one of"):
            surface_errors(boom)()

    def test_success_is_returned_untouched(self) -> None:
        assert surface_errors(lambda: "Response: ok")() == "Response: ok"

    @pytest.mark.parametrize(
        "payload",
        [
            "Response: build failed\nError: missing symbol",
            "File: log.txt\nError: an earlier failure quoted in a file",
            "main.py:12: raise ValueError('Error: bad input')",
        ],
    )
    def test_the_word_error_further_down_is_not_a_failure(self, payload: str) -> None:
        """A build log or a grep hit is a successful call, not a tool error."""
        assert looks_like_failure(payload) is False
        assert surface_errors(lambda: payload)() == payload

    def test_non_string_results_pass_through(self) -> None:
        assert surface_errors(lambda: {"ok": True})() == {"ok": True}

    async def test_async_tools_are_wrapped_too(self) -> None:
        async def failing() -> str:
            return "Error: the worker did not start"

        with pytest.raises(ToolError, match="did not start"):
            await surface_errors(failing)()

    def test_the_signature_survives_wrapping(self) -> None:
        """FastMCP builds the input schema from this signature."""

        def demo(mode: str = "read", url: str | None = None) -> str:
            return "ok"

        assert inspect.signature(surface_errors(demo)) == inspect.signature(demo)

    def test_the_proxy_applies_the_wrapper(self) -> None:
        fake = _FakeMCP()
        proxy = CategorisedMCP(fake, "coding")

        @proxy.tool(name="Demo", description="Fails.")
        def demo() -> str:
            return "Error: broken"

        registered = fake.registered[0]["fn"]
        with pytest.raises(ToolError, match="broken"):
            registered()
