"""Precise Python symbol outlines, backed by the real Python parser.

Why this module exists
----------------------
``Grep mode='outline'`` matches declarations with regular expressions. That is
the right trade-off for twenty languages at once, but it lies about Python: a
bare decorator counts as its own "symbol", a signature wrapped over four lines
shows up as a truncated first line, a ``def`` inside a docstring or a
commented-out block is reported as real code, and nothing tells the caller
whether ``def save`` is a module-level function or the third method of the
fifth class. Python ships an exact parser, so for ``.py``/``.pyi`` we use it:
real nesting, collapsed signatures, line spans, decorators attached to the
thing they decorate, and no false positives.

Why not tree-sitter
-------------------
tree-sitter would buy the same precision for TypeScript, Go and Rust, but it is
a compiled dependency with a separate grammar wheel per language, and this
server deliberately keeps its import graph small enough that no third-party
package can stop it from starting - the same reason the search stack runs
out-of-process. The regex scanner stays as the fallback for every other
language; this module makes the most common case here exact.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["SUPPORTED_SUFFIXES", "Symbol", "outline"]

SUPPORTED_SUFFIXES = frozenset({".py", ".pyi"})

MAX_ANNOTATION = 40
MAX_VALUE = 40
MAX_DECORATOR = 40
MAX_BASE = 40

_TYPE_ALIAS = getattr(ast, "TypeAlias", None)
_CONTROL_FLOW = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)


@dataclass(frozen=True)
class Symbol:
    """One declaration, already rendered as a single readable line."""

    line: int
    end_line: int
    depth: int
    kind: str
    text: str


def _short(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 3)] + "..."


def _unparse(node: ast.AST | None, limit: int) -> str:
    if node is None:
        return ""
    try:
        return _short(ast.unparse(node), limit)
    except Exception:  # pragma: no cover - unparse is total in practice
        return "..."


def _end(node: ast.AST) -> int:
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)


def _parameters(args: ast.arguments) -> str:
    """Render a signature compactly: names and markers, defaults as ``=...``.

    The point of an outline is to answer "what can I call and with what", not to
    reproduce the source. Real default values are often long expressions that
    would push the useful part off the line.
    """
    parts: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    defaults = list(args.defaults)
    first_default = len(positional) - len(defaults)
    for index, argument in enumerate(positional):
        piece = argument.arg
        annotation = _unparse(argument.annotation, MAX_ANNOTATION)
        if annotation:
            piece += f": {annotation}"
        if index >= first_default:
            piece += "=..."
        parts.append(piece)
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg is not None:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        piece = argument.arg
        annotation = _unparse(argument.annotation, MAX_ANNOTATION)
        if annotation:
            piece += f": {annotation}"
        if default is not None:
            piece += "=..."
        parts.append(piece)
    if args.kwarg is not None:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _decorators(node: ast.AST) -> str:
    rendered = [
        f"@{_unparse(item, MAX_DECORATOR)}"
        for item in getattr(node, "decorator_list", None) or []
    ]
    return " ".join(item for item in rendered if item != "@")


def _render_function(node: ast.AST, kind: str) -> str:
    text = f"{kind} {node.name}({_parameters(node.args)})"
    returns = _unparse(node.returns, MAX_ANNOTATION)
    if returns:
        text += f" -> {returns}"
    decorators = _decorators(node)
    if decorators:
        text += f"  {decorators}"
    return text


def _render_class(node: ast.ClassDef) -> str:
    bases = [_unparse(base, MAX_BASE) for base in node.bases]
    bases += [
        f"{keyword.arg or '**'}={_unparse(keyword.value, MAX_BASE)}"
        for keyword in node.keywords
    ]
    text = f"class {node.name}"
    if bases:
        text += "(" + ", ".join(bases) + ")"
    decorators = _decorators(node)
    if decorators:
        text += f"  {decorators}"
    return text


def _render_assignment(node: ast.AST) -> str | None:
    """Render a module- or class-level binding, or None when it is not a name."""
    if isinstance(node, ast.AnnAssign):
        if not isinstance(node.target, ast.Name):
            return None
        text = node.target.id
        annotation = _unparse(node.annotation, MAX_ANNOTATION)
        if annotation:
            text += f": {annotation}"
        if node.value is not None:
            text += f" = {_unparse(node.value, MAX_VALUE)}"
        return text
    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
    if not names:
        return None
    return f"{' = '.join(names)} = {_unparse(node.value, MAX_VALUE)}"


def _walk(body, depth: int, out: list[Symbol], *, assignments: bool) -> None:
    """Collect declarations from a body.

    ``assignments`` is False inside function bodies: locals are not part of a
    module's shape, so only nested functions and classes are followed there.
    Control-flow blocks are walked at the *same* depth, because a class behind
    ``if TYPE_CHECKING:`` or a ``try: import`` guard is still a top-level name.
    """
    for node in body:
        if isinstance(node, ast.ClassDef):
            out.append(Symbol(node.lineno, _end(node), depth, "class", _render_class(node)))
            _walk(node.body, depth + 1, out, assignments=True)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append(Symbol(node.lineno, _end(node), depth, kind, _render_function(node, kind)))
            _walk(node.body, depth + 1, out, assignments=False)
        elif assignments and isinstance(node, (ast.Assign, ast.AnnAssign)):
            text = _render_assignment(node)
            if text is not None:
                out.append(Symbol(node.lineno, _end(node), depth, "assign", text))
        elif _TYPE_ALIAS is not None and isinstance(node, _TYPE_ALIAS):
            name = getattr(node.name, "id", None) or _unparse(node.name, MAX_BASE)
            value = _unparse(node.value, MAX_VALUE)
            out.append(
                Symbol(node.lineno, _end(node), depth, "type", f"type {name} = {value}")
            )
        elif isinstance(node, _CONTROL_FLOW):
            _walk(node.body, depth, out, assignments=assignments)
            _walk(getattr(node, "orelse", None) or [], depth, out, assignments=assignments)
            _walk(getattr(node, "finalbody", None) or [], depth, out, assignments=assignments)
            for handler in getattr(node, "handlers", None) or []:
                _walk(handler.body, depth, out, assignments=assignments)


def outline(source: str) -> list[Symbol]:
    """Return the declarations in ``source``, in file order.

    Raises ``SyntaxError`` when the source does not parse. Callers are expected
    to fall back to pattern matching *and say so*: silently returning an empty
    outline for a file that is merely mid-edit is worse than a slightly wrong
    outline, because the caller cannot tell the two apart.
    """
    tree = ast.parse(source)
    found: list[Symbol] = []
    _walk(tree.body, 0, found, assignments=True)
    found.sort(key=lambda item: (item.line, item.depth))
    return found
