"""Tests for the AST-backed Python outline.

These pin the two things the real parser buys over regular expressions: exact
nesting, and no false positives from strings, comments or wrapped signatures.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from windows_mcp.coding import ast_outline, grep_service

SAMPLE = textwrap.dedent(
    '''
    """Module docstring that mentions def ghost() and class Phantom."""

    MAX_ITEMS = 42
    TIMEOUT: float = 1.5


    def helper(
        first,
        second=3,
        *rest,
        key=None,
        **extra,
    ) -> str:
        local_value = 1

        def inner():
            return local_value

        return str(inner())


    class Widget(Base, metaclass=Meta):
        columns = ("a", "b")

        @property
        def size(self) -> int:
            return len(self.columns)

        async def refresh(self, *, force: bool = False) -> None:
            scratch = 1
            del scratch
    '''
).lstrip("\n")


def _texts(source: str) -> list[str]:
    return [item.text for item in ast_outline.outline(source)]


def _find(source: str, prefix: str) -> ast_outline.Symbol:
    for item in ast_outline.outline(source):
        if item.text.startswith(prefix):
            return item
    raise AssertionError(f"no symbol starting with {prefix!r}")


class TestSymbols:
    def test_wrapped_signature_collapses_to_one_line(self):
        assert _find(SAMPLE, "def helper").text == (
            "def helper(first, second=..., *rest, key=..., **extra) -> str"
        )

    def test_depth_separates_methods_from_module_functions(self):
        assert _find(SAMPLE, "def helper").depth == 0
        assert _find(SAMPLE, "def inner").depth == 1
        assert _find(SAMPLE, "class Widget").depth == 0
        assert _find(SAMPLE, "def size").depth == 1
        assert _find(SAMPLE, "async def refresh").depth == 1

    def test_decorators_stay_attached_instead_of_becoming_symbols(self):
        assert _find(SAMPLE, "def size").text == "def size(self) -> int  @property"
        assert not any(item.startswith("@") for item in _texts(SAMPLE))

    def test_class_bases_and_keywords_are_kept(self):
        assert _find(SAMPLE, "class Widget").text == "class Widget(Base, metaclass=Meta)"

    def test_module_and_class_level_bindings_are_listed(self):
        texts = _texts(SAMPLE)
        assert "MAX_ITEMS = 42" in texts
        assert "TIMEOUT: float = 1.5" in texts
        assert "columns = ('a', 'b')" in texts

    def test_locals_are_not_symbols(self):
        texts = _texts(SAMPLE)
        assert not any(item.startswith("local_value") for item in texts)
        assert not any(item.startswith("scratch") for item in texts)

    def test_definitions_inside_strings_are_not_symbols(self):
        """Regression: the regex scanner reported docstring examples as code."""
        assert not any("ghost" in item for item in _texts(SAMPLE))
        assert not any("Phantom" in item for item in _texts(SAMPLE))

    def test_line_span_covers_the_whole_body(self):
        widget = _find(SAMPLE, "class Widget")
        assert widget.end_line > widget.line

    def test_declarations_behind_a_type_checking_guard_are_top_level(self):
        source = "import typing\nif typing.TYPE_CHECKING:\n    class Only: ...\n"
        assert _find(source, "class Only").depth == 0

    def test_declarations_in_a_try_import_guard_are_found(self):
        source = "try:\n    import fast\nexcept ImportError:\n    def fallback(): ...\n"
        assert _find(source, "def fallback").depth == 0

    def test_broken_source_raises_instead_of_reporting_nothing(self):
        with pytest.raises(SyntaxError):
            ast_outline.outline("def broken(:\n    pass\n")

    @pytest.mark.skipif(not hasattr(ast, "TypeAlias"), reason="needs Python 3.12+")
    def test_type_alias(self):
        assert "type Ids = list[int]" in _texts("type Ids = list[int]\n")


class TestGrepIntegration:
    def test_python_files_use_the_parser(self, tmp_path):
        path = tmp_path / "widget.py"
        path.write_text(SAMPLE, encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "python ast" in out
        assert "class Widget(Base, metaclass=Meta)" in out
        assert "|   def size(self) -> int  @property" in out
        assert "ghost" not in out

    def test_unparsable_python_falls_back_and_says_so(self, tmp_path):
        path = tmp_path / "broken.py"
        path.write_text("def ok():\n    pass\n\ndef broken(:\n", encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "does not parse" in out
        assert "def ok" in out
        assert "python ast" not in out

    def test_pyi_stubs_are_parsed_too(self, tmp_path):
        path = tmp_path / "stub.pyi"
        path.write_text("class A:\n    def b(self) -> int: ...\n", encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "python ast" in out
        assert "|   def b(self) -> int" in out

    def test_other_languages_still_use_patterns(self, tmp_path):
        path = tmp_path / "app.ts"
        path.write_text("export function alpha() {}\n", encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "python ast" not in out
        assert "export function alpha" in out
