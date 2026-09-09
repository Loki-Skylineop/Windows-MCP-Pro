"""Tests for windows_mcp.coding.grep_service (content search, maps, outlines)."""

import os

import pytest

from windows_mcp.coding import grep_service


@pytest.fixture()
def tree(tmp_path):
    source = tmp_path / "src" / "deep"
    source.mkdir(parents=True)
    big = source / "big.ts"
    lines = ["export function alpha() { return 1 }"]
    lines += [f"export const item_{index} = 'value-{index}' // NEEDLE_{index % 7}" for index in range(1, 201)]
    lines += [
        "export const handler = async (event) => {",
        "  return event",
        "}",
        "export class Beta {",
        "  method() { return 2 }",
        "}",
    ]
    big.write_text("\n".join(lines) + "\n", encoding="utf-8")

    module = tmp_path / "mod.py"
    module.write_text(
        "import os\n\n\nclass Widget:\n    def build(self):\n        return NEEDLE_1\n",
        encoding="utf-8",
    )

    decoy = tmp_path / "node_modules" / "pkg"
    decoy.mkdir(parents=True)
    (decoy / "index.ts").write_text("NEEDLE_1 must never be found\n", encoding="utf-8")

    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "config.ts").write_text("NEEDLE_1 in git internals\n", encoding="utf-8")

    return tmp_path, str(big), str(module)


class TestGrep:
    def test_finds_content(self, tree):
        root, _, _ = tree
        out = grep_service.grep(str(root), "NEEDLE_1", glob="*.ts,*.py")
        assert "big.ts:" in out
        assert "mod.py:" in out
        assert "match(es)" in out

    def test_skips_noise_directories(self, tree):
        root, _, _ = tree
        out = grep_service.grep(str(root), "NEEDLE_1")
        assert "node_modules" not in out
        assert ".git" not in out

    def test_literal_and_context(self, tree):
        root, _, _ = tree
        out = grep_service.grep(str(root), "export const item_5 ", literal=True, context=2, glob="*.ts")
        assert out.count("big.ts:") >= 3

    def test_ignore_case_and_cap(self, tree):
        root, _, _ = tree
        out = grep_service.grep(str(root), "needle_3", ignore_case=True, max_results=4, glob="*.ts")
        assert "4 match(es)" in out

    def test_no_matches(self, tree):
        root, _, _ = tree
        assert "No matches." in grep_service.grep(str(root), "ZZZ-NOT-PRESENT-ZZZ")

    def test_invalid_regex(self, tree):
        root, _, _ = tree
        assert grep_service.grep(str(root), "[unclosed").startswith("Error:")

    def test_missing_root(self, tmp_path):
        assert grep_service.grep(str(tmp_path / "ghost"), "x").startswith("Error:")

    def test_single_file_target(self, tree):
        _, big, _ = tree
        out = grep_service.grep(big, "class Beta")
        assert "big.ts:" in out and "1 match(es)" in out

    def test_multiline(self, tree):
        _, big, _ = tree
        out = grep_service.grep(big, r"handler = async \(event\) => \{\n  return", multiline=True)
        assert "1 match(es)" in out


class TestRepoMap:
    def test_totals_and_ranking(self, tree):
        root, _, _ = tree
        out = grep_service.repo_map(str(root))
        assert "Totals:" in out
        assert ".ts" in out and ".py" in out
        assert "big.ts" in out
        assert "node_modules" not in out

    def test_not_a_directory(self, tree):
        _, big, _ = tree
        assert grep_service.repo_map(big).startswith("Error:")


class TestOutline:
    def test_python(self, tree):
        _, _, module = tree
        out = grep_service.outline(module)
        assert "class Widget" in out
        assert "def build" in out

    def test_typescript_ignores_value_constants(self, tree):
        """Regression: value constants used to flood the outline and hide real symbols."""
        _, big, _ = tree
        out = grep_service.outline(big)
        assert "export function alpha" in out
        assert "export class Beta" in out
        assert "export const handler" in out
        assert "item_5" not in out
        assert len(out.splitlines()) < 20

    def test_missing_file(self, tmp_path):
        assert grep_service.outline(str(tmp_path / "ghost.ts")).startswith("Error:")

    def test_unknown_extension_uses_generic_patterns(self, tmp_path):
        path = tmp_path / "thing.unknown"
        path.write_text("function helper() {}\nnoise\n", encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "function helper" in out

    def test_markdown_headings(self, tmp_path):
        path = tmp_path / "readme.md"
        path.write_text("# Title\n\ntext\n\n## Section\n", encoding="utf-8")
        out = grep_service.outline(str(path))
        assert "# Title" in out and "## Section" in out


def test_default_skip_dirs_are_lowercase():
    assert all(name == name.lower() for name in grep_service.DEFAULT_SKIP_DIRS)
    assert "node_modules" in grep_service.DEFAULT_SKIP_DIRS
    assert os.sep not in "".join(grep_service.DEFAULT_SKIP_DIRS)
