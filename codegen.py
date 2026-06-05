"""Code generator: convert Python AST to source code.

Uses ast.unparse() (Python 3.9+) for clean output, with formatting
tweaks for decompiled code.
"""

from __future__ import annotations

import ast
from typing import Optional


def _fix_missing_locations(node: ast.AST, lineno: int = 1, col_offset: int = 0) -> None:
    """Set lineno and col_offset on AST nodes that lack them.

    Uses iterative traversal to avoid recursion depth issues with deeply
    nested ASTs (e.g., large classes with many methods).
    """
    stack = [node]
    while stack:
        n = stack.pop()
        if not hasattr(n, 'lineno') or n.lineno is None:
            n.lineno = lineno
            n.col_offset = col_offset
            n.end_lineno = lineno
            n.end_col_offset = col_offset + 1
        # Extend stack with children (reverse order for consistent traversal)
        children = list(ast.iter_child_nodes(n))
        stack.extend(reversed(children))


def generate_source(node: ast.AST) -> str:
    """Generate Python source code from an AST node.

    Args:
        node: An ast.Module, ast.FunctionDef, or other AST node.

    Returns:
        Formatted Python source code string.
    """
    if node is None:
        return ""

    # Ensure all nodes have lineno (required by ast.unparse in 3.12+)
    _fix_missing_locations(node)

    try:
        source = ast.unparse(node)
    except Exception:
        source = _fallback_unparse(node)

    return _format_source(source)


def _format_source(source: str) -> str:
    """Apply basic formatting to decompiled source code."""
    lines = source.split("\n")

    # Remove trailing whitespace
    lines = [line.rstrip() for line in lines]

    # Remove blank lines at start/end
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines) + "\n"


def _fallback_unparse(node: ast.AST) -> str:
    """Fallback AST pretty-printer for Python < 3.9."""
    # Very basic fallback — won't happen on Python 3.12 host
    if isinstance(node, ast.Module):
        return "\n".join(
            _fallback_unparse(stmt) for stmt in node.body
        )
    if isinstance(node, ast.Expr):
        return _fallback_unparse(node.value)
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        func_str = _fallback_unparse(node.func)
        args_str = ", ".join(_fallback_unparse(a) for a in node.args)
        return f"{func_str}({args_str})"
    if isinstance(node, ast.FunctionDef):
        args_str = _fallback_unparse(node.args)
        body_str = "\n".join(
            "    " + _fallback_unparse(s) for s in node.body
        )
        return f"def {node.name}({args_str}):\n{body_str}"
    if isinstance(node, ast.Return):
        if node.value:
            return f"return {_fallback_unparse(node.value)}"
        return "return"
    if isinstance(node, ast.Assign):
        targets = ", ".join(_fallback_unparse(t) for t in node.targets)
        value = _fallback_unparse(node.value)
        return f"{targets} = {value}"
    if isinstance(node, ast.Import):
        names = ", ".join(a.name for a in node.names)
        return f"import {names}"
    if isinstance(node, ast.ImportFrom):
        names = ", ".join(a.name for a in node.names)
        return f"from {node.module} import {names}"
    if isinstance(node, ast.Pass):
        return "pass"
    if isinstance(node, ast.BinOp):
        left = _fallback_unparse(node.left)
        right = _fallback_unparse(node.right)
        op = _op_str(node.op)
        return f"({left} {op} {right})"
    if isinstance(node, ast.If):
        test = _fallback_unparse(node.test)
        body = "\n".join("    " + _fallback_unparse(s) for s in node.body)
        result = f"if {test}:\n{body}"
        if node.orelse:
            else_body = "\n".join("    " + _fallback_unparse(s) for s in node.orelse)
            result += f"\nelse:\n{else_body}"
        return result
    return f"<{type(node).__name__}>"


def _op_str(op: ast.operator) -> str:
    """Convert AST operator to string."""
    mapping = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
        ast.LShift: "<<", ast.RShift: ">>",
        ast.BitOr: "|", ast.BitXor: "^", ast.BitAnd: "&",
        ast.MatMult: "@",
    }
    return mapping.get(type(op), "?")
