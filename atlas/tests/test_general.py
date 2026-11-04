import json
import math
import time

import pytest

from app.tools import ToolError, call_tool
from app.tools.general import safe_eval
from conftest import make_ctx, run


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("2+2", 4),
        ("10 - 3 * 2", 4),
        ("7 / 2", 3.5),
        ("7 // 2", 3),
        ("7 % 4", 3),
        ("-3 + +5", 2),
        ("2^10", 1024),
        ("2**10", 1024),
        ("(1+2)*3", 9),
        ("6 × 7", 42),
        ("84 ÷ 2", 42),
        ("1,500 * 2", 3000),
        ("1,500,000 + 2", 1_500_002),
        ("sqrt(16)", 4.0),
        ("max(1, 2)", 2),
        ("min(3, 1, 2)", 1),
        ("round(3.14159, 2)", 3.14),
        ("round(2.6)", 3),
        ("hypot(3, 4)", 5.0),
        ("hypot(3,4)", 5.0),
        ("gcd(12, 18)", 6),
        ("log(8, 2)", 3.0),
        ("log10(1000)", 3.0),
        ("pow(2, 10)", 1024),
        ("pow(2, 10, 1000)", 24),
        ("abs(-5)", 5),
        ("factorial(5)", 120),
        ("floor(2.7) + ceil(2.1)", 5),
        ("max(1,500, 2)", 500),
        ("sqrt(1,000,000)", None),  # thousands separators inside a call are arguments, not digits
    ],
)
def test_safe_eval_values(expr, expected):
    if expected is None:
        with pytest.raises((ToolError, TypeError)):
            safe_eval(expr)
        return
    assert safe_eval(expr) == pytest.approx(expected)


def test_constants_and_compound_growth():
    assert safe_eval("pi") == pytest.approx(math.pi)
    assert safe_eval("e") == pytest.approx(math.e)
    assert safe_eval("tau / 2") == pytest.approx(math.pi)
    assert safe_eval("1500*(1+0.07)^10") == pytest.approx(1500 * 1.07**10)
    assert safe_eval("sin(pi/2)") == pytest.approx(1.0)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",
        "__import__('os').system('echo hi')",
        "os.system('ls')",
        "(1).__class__",
        "().__class__.__bases__[0]",
        "open('/etc/passwd')",
        "eval('1+1')",
        "x + 1",
        "lambda: 1",
        "[1, 2]",
        "'abc' * 3",
        "[x for x in range(3)]",
        "{1: 2}",
        "1 if 1 else 2",
        "1 < 2",
        "2 << 3",
        "math.sqrt(4)",
        "round(2.5, ndigits=1)",
        "1j * 2",
        "1,5",
    ],
)
def test_safe_eval_rejects_non_math(expr):
    with pytest.raises((ToolError, SyntaxError)):
        safe_eval(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "2 ** 100000",
        "10 ** 10001",
        "9999 ** 9999",
        "(9 ** 9999) ** 9999",
        "pow(9999, 9999999)",
        "factorial(100000)",
        "round(5, -1000000000)",
        "(2 ** 6000) * (2 ** 6000)",
    ],
)
def test_huge_results_are_refused_quickly(expr):
    start = time.perf_counter()
    with pytest.raises(ToolError):
        safe_eval(expr)
    assert time.perf_counter() - start < 1.0


def test_reasonably_large_results_still_work():
    assert safe_eval("2 ** 1000") == 2**1000
    assert safe_eval("factorial(100)") == math.factorial(100)
    assert safe_eval("2 ** -3") == 0.125


def calc(expression):
    return json.loads(run(call_tool("calculate", {"expression": expression}, make_ctx())))


def test_calculate_tool_results():
    assert calc("2+2") == {"expression": "2+2", "result": 4}
    assert calc("10/2")["result"] == 5 and isinstance(calc("10/2")["result"], int)
    assert calc("7/2")["result"] == 3.5


@pytest.mark.parametrize(
    "expression, message",
    [
        ("1/0", "Division by zero."),
        ("5 % 0", "Division by zero."),
        ("2 +* 3", "isn't valid math syntax"),
        ("sqrt(-1)", "Math error"),
        ("log(0)", "Math error"),
        ("exp(1000)", "Result too large."),
        ("2 ** 100000", "Exponent too large."),
        ("10 ** 5000", "Result too large."),
        ("(-8) ** 0.5", "isn't a real number"),
        ("__import__('os')", "Unsupported expression"),
    ],
)
def test_calculate_tool_errors(expression, message):
    out = calc(expression)
    assert "error" in out, out
    assert message in out["error"]


def test_get_time():
    out = json.loads(run(call_tool("get_time", {"timezone": "Asia/Tokyo"}, make_ctx())))
    assert out["timezone"] == "Asia/Tokyo" and out["utc_offset"] == "+0900"
    bad = json.loads(run(call_tool("get_time", {"timezone": "Mars/Olympus"}, make_ctx())))
    assert "Unknown timezone" in bad["error"]
