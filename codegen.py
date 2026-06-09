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


def _indent_lines(text: str, spaces: int = 4) -> str:
    """Indent all lines of *text* by *spaces* spaces."""
    ind = " " * spaces
    return ind + text.replace("\n", "\n" + ind)


def _fallback_unparse(node: ast.AST) -> str:
    """Fallback AST pretty-printer for Python < 3.9."""
    body_indent = "    "

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
    if isinstance(node, ast.List):
        elts = ", ".join(_fallback_unparse(e) for e in node.elts)
        return f"[{elts}]"
    if isinstance(node, ast.Tuple):
        elts = ", ".join(_fallback_unparse(e) for e in node.elts)
        return f"({elts})"
    if isinstance(node, ast.Dict):
        pairs = []
        for k, v in zip(node.keys, node.values):
            pairs.append(f"{_fallback_unparse(k)}: {_fallback_unparse(v)}")
        return "{" + ", ".join(pairs) + "}"
    if isinstance(node, ast.Set):
        elts = ", ".join(_fallback_unparse(e) for e in node.elts)
        return "{" + elts + "}"
    if isinstance(node, ast.ClassDef):
        bases_str = ""
        if node.bases:
            bases_str = "(" + ", ".join(_fallback_unparse(b) for b in node.bases) + ")"
        body = node.body if node.body else [ast.Pass()]
        body_lines = []
        for s in body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        body_str = "\n".join(body_lines)
        if not body_str.strip():
            body_str = "    pass"
        return f"class {node.name}{bases_str}:\n{body_str}"
    if isinstance(node, ast.FunctionDef):
        args = node.args
        parts = []
        for a in args.args:
            parts.append(a.arg)
        if args.vararg:
            parts.append(f"*{args.vararg.arg}")
        for a in args.kwonlyargs:
            parts.append(a.arg)
        if args.kwarg:
            parts.append(f"**{args.kwarg.arg}")
        args_str = ", ".join(parts)
        body = node.body if node.body else [ast.Pass()]
        body_lines = []
        for s in body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        body_str = "\n".join(body_lines)
        if not body_str.strip():
            body_str = "    pass"
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
    if isinstance(node, ast.Attribute):
        return f"{_fallback_unparse(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_fallback_unparse(node.value)}[{_fallback_unparse(node.slice)}]"
    if isinstance(node, ast.Slice):
        parts = []
        lower = getattr(node, 'lower', None)
        upper = getattr(node, 'upper', None)
        step = getattr(node, 'step', None)
        if lower:
            parts.append(_fallback_unparse(lower))
        if upper:
            parts.append(_fallback_unparse(upper))
        if step:
            parts.append(_fallback_unparse(step))
        return ":".join(parts)
    if isinstance(node, ast.UnaryOp):
        op = {ast.USub: '-', ast.UAdd: '+', ast.Not: 'not ', ast.Invert: '~'}.get(type(node.op), '?')
        return f"{op}{_fallback_unparse(node.operand)}"
    if isinstance(node, ast.Compare):
        left = _fallback_unparse(node.left)
        ops = []
        for op, comp in zip(node.ops, node.comparators):
            op_str = _op_str(op) if isinstance(op, ast.cmpop) else '?'
            ops.append(f"{op_str} {_fallback_unparse(comp)}")
        return f"{left} {' '.join(ops)}"
    if isinstance(node, ast.BoolOp):
        op = ' and ' if isinstance(node.op, ast.And) else ' or '
        return op.join(_fallback_unparse(v) for v in node.values)
    if isinstance(node, ast.IfExp):
        return f"{_fallback_unparse(node.body)} if {_fallback_unparse(node.test)} else {_fallback_unparse(node.orelse)}"
    if isinstance(node, ast.BinOp):
        left = _fallback_unparse(node.left)
        right = _fallback_unparse(node.right)
        op = _op_str(node.op)
        return f"({left} {op} {right})"
    if isinstance(node, ast.If):
        test = _fallback_unparse(node.test)
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        body = "\n".join(body_lines)
        result = f"if {test}:\n{body}"
        if node.orelse:
            else_lines = []
            for s in node.orelse:
                else_lines.append(_indent_lines(_fallback_unparse(s)))
            result += f"\nelse:\n" + "\n".join(else_lines)
        return result
    if isinstance(node, ast.AugAssign):
        target = _fallback_unparse(node.target)
        op = _op_str(node.op)
        value = _fallback_unparse(node.value)
        return f"{target} {op}= {value}"
    if isinstance(node, ast.FormattedValue):
        val = _fallback_unparse(node.value)
        conv_map = {-1: '', 0: '', 1: '!s', 2: '!r', 3: '!a'}
        conv = conv_map.get(node.conversion, '')
        fmt = f":{_fallback_unparse(node.format_spec)}" if node.format_spec else ''
        return f"{{{val}{conv}{fmt}}}"
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                parts.append(_fallback_unparse(v))
            elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(_fallback_unparse(v))
        return "f'" + "".join(parts) + "'"
    if isinstance(node, ast.Try):
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        result = "try:\n" + "\n".join(body_lines)
        for h in node.handlers:
            result += "\n" + _fallback_unparse(h)
        if node.orelse:
            else_lines = []
            for s in node.orelse:
                else_lines.append(_indent_lines(_fallback_unparse(s)))
            result += "\nelse:\n" + "\n".join(else_lines)
        if node.finalbody:
            fb_lines = []
            for s in node.finalbody:
                fb_lines.append(_indent_lines(_fallback_unparse(s)))
            result += "\nfinally:\n" + "\n".join(fb_lines)
        return result
    if isinstance(node, ast.ExceptHandler):
        t = _fallback_unparse(node.type) if node.type else ''
        name = f" as {node.name}" if node.name else ''
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        return f"except {t}{name}:\n" + "\n".join(body_lines)
    if isinstance(node, ast.Raise):
        exc = _fallback_unparse(node.exc) if node.exc else ''
        cause = f" from {_fallback_unparse(node.cause)}" if node.cause else ''
        return f"raise {exc}{cause}".strip()
    if isinstance(node, ast.Expr):
        return _fallback_unparse(node.value)
    if isinstance(node, ast.For):
        target = _fallback_unparse(node.target)
        iter_ = _fallback_unparse(node.iter)
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        result = f"for {target} in {iter_}:\n" + "\n".join(body_lines)
        if node.orelse:
            else_lines = []
            for s in node.orelse:
                else_lines.append(_indent_lines(_fallback_unparse(s)))
            result += "\nelse:\n" + "\n".join(else_lines)
        return result
    if isinstance(node, ast.While):
        test = _fallback_unparse(node.test)
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        result = f"while {test}:\n" + "\n".join(body_lines)
        if node.orelse:
            else_lines = []
            for s in node.orelse:
                else_lines.append(_indent_lines(_fallback_unparse(s)))
            result += "\nelse:\n" + "\n".join(else_lines)
        return result
    if isinstance(node, ast.With):
        items = ", ".join(
            f"{_fallback_unparse(it.context_expr)}"
            + (f" as {_fallback_unparse(it.optional_vars)}" if it.optional_vars else '')
            for it in node.items
        )
        body_lines = []
        for s in node.body:
            body_lines.append(_indent_lines(_fallback_unparse(s)))
        return f"with {items}:\n" + "\n".join(body_lines)
    if isinstance(node, ast.Starred):
        return f"*{_fallback_unparse(node.value)}"
    if isinstance(node, ast.Yield):
        return f"yield {_fallback_unparse(node.value)}" if node.value else "yield"
    if isinstance(node, ast.YieldFrom):
        return f"yield from {_fallback_unparse(node.value)}"
    if isinstance(node, ast.Lambda):
        args_str = ", ".join(a.arg for a in node.args.args)
        body = _fallback_unparse(node.body)
        return f"lambda {args_str}: {body}"
    if isinstance(node, ast.arguments):
        parts = [a.arg for a in node.args]
        if node.vararg:
            parts.append(f"*{node.vararg.arg}")
        parts.extend(a.arg for a in node.kwonlyargs)
        if node.kwarg:
            parts.append(f"**{node.kwarg.arg}")
        return ", ".join(parts)
    if isinstance(node, ast.keyword):
        v = _fallback_unparse(node.value)
        return f"{node.arg}={v}" if node.arg else f"**{v}"
    if isinstance(node, ast.Delete):
        targets = ", ".join(_fallback_unparse(t) for t in node.targets)
        return f"del {targets}"
    if isinstance(node, ast.Assert):
        test = _fallback_unparse(node.test)
        msg = f", {_fallback_unparse(node.msg)}" if node.msg else ''
        return f"assert {test}{msg}"
    if isinstance(node, ast.Global):
        return f"global {', '.join(node.names)}"
    if isinstance(node, ast.Nonlocal):
        return f"nonlocal {', '.join(node.names)}"
    if isinstance(node, ast.Pass):
        return "pass"
    if isinstance(node, ast.Break):
        return "break"
    if isinstance(node, ast.Continue):
        return "continue"
    if isinstance(node, ast.Call):
        args = ", ".join(_fallback_unparse(a) for a in node.args)
        kwargs = ", ".join(_fallback_unparse(k) for k in node.keywords)
        all_args = ", ".join(filter(None, [args, kwargs]))
        return f"{_fallback_unparse(node.func)}({all_args})"
    return f"<{type(node).__name__}>"


def _op_str(op: ast.AST) -> str:
    """Convert AST operator to string."""
    mapping = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
        ast.LShift: "<<", ast.RShift: ">>",
        ast.BitOr: "|", ast.BitXor: "^", ast.BitAnd: "&",
        ast.MatMult: "@",
        ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!=",
        ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not",
        ast.In: "in", ast.NotIn: "not in",
    }
    return mapping.get(type(op), "?")
