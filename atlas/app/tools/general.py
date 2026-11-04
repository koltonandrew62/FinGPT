"""General-purpose tools: precise math and world clock."""
from __future__ import annotations

import ast
import math
import operator
import re
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

# Big-integer work runs synchronously on the event loop, so keep results to a size that computes and
# prints instantly (~3,000 digits; Python refuses to stringify ints over 4,300 digits anyway).
MAX_INT_BITS = 10_000
MAX_FACTORIAL = 1000


def _check_pow(base, exp) -> None:
    if abs(exp) > 10_000:
        raise ToolError("Exponent too large.")
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and abs(base) > 1:
        if exp * math.log2(abs(base)) > MAX_INT_BITS:
            raise ToolError("Result too large.")


def _pow(base, exp, mod=None):
    if mod is not None:
        return pow(base, exp, mod)  # modular exponentiation stays small and fast
    _check_pow(base, exp)
    return pow(base, exp)


def _factorial(n):
    if n > MAX_FACTORIAL:
        raise ToolError(f"factorial is limited to n <= {MAX_FACTORIAL}.")
    return math.factorial(n)


def _round(x, ndigits=None):
    if ndigits is None:
        return round(x)
    if abs(ndigits) > 100:
        raise ToolError("round() precision must be between -100 and 100.")
    return round(x, ndigits)


_FUNCS = {
    name: getattr(math, name)
    for name in (
        "sqrt", "log", "log10", "log2", "exp", "sin", "cos", "tan", "asin", "acos", "atan",
        "floor", "ceil", "radians", "degrees", "hypot", "gcd",
    )
}
_FUNCS.update({"abs": abs, "round": _round, "min": min, "max": max, "pow": _pow, "factorial": _factorial})
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_CALL_BEFORE = re.compile(r"[A-Za-z_]\w*\s*$")
_THOUSANDS = re.compile(r"\d{3}(?!\d)")


def _strip_thousands(expr: str) -> str:
    """Drop thousands separators ("1,500,000") but keep commas that separate function arguments."""
    out: list[str] = []
    in_call: list[bool] = []  # one entry per open parenthesis: did it open a function call?
    for i, ch in enumerate(expr):
        if ch == "(":
            in_call.append(bool(_CALL_BEFORE.search("".join(out))))
        elif ch == ")" and in_call:
            in_call.pop()
        elif (
            ch == ","
            and not (in_call and in_call[-1])
            and out
            and out[-1].isdigit()
            and _THOUSANDS.match(expr, i + 1)
        ):
            continue
        out.append(ch)
    return "".join(out)


def safe_eval(expression: str) -> float | int:
    expr = _strip_thousands(expression.replace("^", "**").replace("×", "*").replace("÷", "/"))
    tree = ast.parse(expr.strip(), mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow):
                _check_pow(left, right)
            elif isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int):
                if left.bit_length() + right.bit_length() > MAX_INT_BITS:
                    raise ToolError("Result too large.")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            if node.keywords:
                raise ToolError("Keyword arguments aren't supported; pass values positionally.")
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
    except OverflowError:
        raise ToolError("Result too large.")
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Math error: {exc}")
    if isinstance(value, complex):
        raise ToolError("The result isn't a real number.")
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
