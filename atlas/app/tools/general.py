"""General-purpose tools: precise math and world clock."""
from __future__ import annotations

import ast
import math
import operator
from datetime import datetime

from . import ToolError, tool

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    name: getattr(math, name)
    for name in (
        "sqrt", "log", "log10", "log2", "exp", "sin", "cos", "tan", "asin", "acos", "atan",
        "floor", "ceil", "factorial", "radians", "degrees", "hypot", "gcd",
    )
}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "pow": pow})
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expression: str) -> float | int:
    expr = expression.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10_000:
                raise ToolError("Exponent too large.")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ToolError(f"Unsupported expression element: {ast.dump(node)[:60]}")

    return ev(tree)


@tool(
    "calculate",
    "Evaluate a math expression exactly (arithmetic, powers, sqrt, log, trig, factorial, pi, e). "
    "Use for any non-trivial arithmetic, percentages, compound growth or unit math.",
    {"expression": {"type": "string", "description": "e.g. '1500*(1+0.07)^10' or 'sqrt(2)*pi'"}},
    ["expression"],
    label="Computing",
)
async def calculate(ctx, expression: str):
    try:
        value = safe_eval(expression)
    except ZeroDivisionError:
        raise ToolError("Division by zero.")
    except SyntaxError:
        raise ToolError("That expression isn't valid math syntax.")
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return {"expression": expression, "result": value}


@tool(
    "get_time",
    "Get the current date and time in a specific IANA timezone or city's zone "
    "(e.g. 'Asia/Tokyo', 'Europe/London'). The user's local time is already in your system prompt.",
    {"timezone": {"type": "string", "description": "IANA timezone name, e.g. 'America/Los_Angeles'"}},
    ["timezone"],
    label="Checking world clock",
)
async def get_time(ctx, timezone: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(f"Unknown timezone '{timezone}'. Use an IANA name like 'Europe/Paris'.")
    return {"timezone": timezone, "local_time": now.strftime("%A, %B %d, %Y %I:%M %p"), "utc_offset": now.strftime("%z")}
