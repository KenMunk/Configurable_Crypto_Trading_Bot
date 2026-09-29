"""Rule expressions for entry and exit signals, e.g. "fast_ema > slow_ema and rsi < 70".

Expressions are parsed with Python's `ast` module against a strict whitelist:
names, numbers, arithmetic, comparisons, and/or/not, and the functions in FUNCTIONS.
Nothing is passed to eval(), so a profile can't run arbitrary code.

Evaluation is vectorized over the whole candle history. Conditions are float arrays
holding 1.0 (true), 0.0 (false) or NaN (unknown, e.g. during an indicator's warm-up);
a rule only fires where it is known to be true.
"""

import ast
import difflib

import numpy as np

from indicators import parse_period, rolling_max, rolling_min, shift

COMPARISONS = {
    ast.Gt: np.greater, ast.GtE: np.greater_equal, ast.Lt: np.less,
    ast.LtE: np.less_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal,
}
ARITHMETIC = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}

# name: (description, argument kinds) where "series" is any expression and
# "periods" is a constant candle count or duration such as "1d".
FUNCTIONS = {
    "crosses_above": ("a moved from at or below b to above b on this candle", ["series", "series"]),
    "crosses_below": ("a moved from at or above b to below b on this candle", ["series", "series"]),
    "previous": ("the value n candles ago (n defaults to 1)", ["series", "periods?"]),
    "change": ("x minus its value n candles ago (n defaults to 1)", ["series", "periods?"]),
    "pct_change": ("fractional change over n candles, e.g. 0.02 for +2% (n defaults to 1)", ["series", "periods?"]),
    "highest": ("the highest value of x over the last n candles, including this one", ["series", "periods"]),
    "lowest": ("the lowest value of x over the last n candles, including this one", ["series", "periods"]),
    "abs": ("absolute value", ["series"]),
    "min": ("the smaller of a and b at each candle", ["series", "series"]),
    "max": ("the larger of a and b at each candle", ["series", "series"]),
}


class RuleError(ValueError):
    pass


def _condition(result, *operands):
    """A comparison result as 1.0/0.0, or NaN where any operand is unknown."""
    out = result.astype(float)
    for operand in operands:
        out[np.isnan(operand)] = np.nan
    return out


def _known_true(condition):
    return np.nan_to_num(condition, nan=0.0) == 1.0


def _and(a, b):
    """Three-valued and: false if either is false, unknown if either is unknown, else true."""
    return np.where((a == 0) | (b == 0), 0.0, np.where(np.isnan(a) | np.isnan(b), np.nan, 1.0))


class Rule:
    def __init__(self, text, known_names, granularity_seconds):
        self.text = text
        self.granularity_seconds = granularity_seconds
        try:
            self.tree = ast.parse(str(text), mode="eval")
        except SyntaxError as exc:
            raise RuleError(f"Rule {text!r} is not a valid formula: {exc.msg}") from None
        self.references = set()
        self._check(self.tree.body, known_names)

    # --- validation -------------------------------------------------------------------

    def _fail(self, message):
        raise RuleError(f"Rule {self.text!r}: {message}")

    def _series_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return None

    def _check(self, node, known):
        name = self._series_name(node)
        if name is not None:
            if name in ("True", "False"):
                return
            if name not in known:
                close = difflib.get_close_matches(name, known, n=3)
                hint = f"; did you mean {', '.join(close)}?" if close else ""
                self._fail(f"unknown name '{name}'{hint} Names come from the profile's indicators and the "
                           "price fields open, high, low, close, volume, hl2, hlc3 and ohlc4.")
            self.references.add(name)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                self._fail(f"{node.value!r} is not a number")
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                self._check(value, known)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            self._check(node.operand, known)
        elif isinstance(node, ast.BinOp) and type(node.op) in ARITHMETIC:
            self._check(node.left, known)
            self._check(node.right, known)
        elif isinstance(node, ast.Compare) and all(type(op) in COMPARISONS for op in node.ops):
            for part in [node.left, *node.comparators]:
                self._check(part, known)
        elif isinstance(node, ast.Call):
            self._check_call(node, known)
        else:
            self._fail(f"'{ast.unparse(node)}' is not allowed; rules may use names, numbers, + - * /, "
                       "comparisons, and/or/not and the rule functions")

    def _check_call(self, node, known):
        function = node.func.id if isinstance(node.func, ast.Name) else None
        if function not in FUNCTIONS:
            self._fail(f"unknown function '{ast.unparse(node.func)}'; available: {', '.join(FUNCTIONS)}")
        if node.keywords:
            self._fail(f"{function}() takes positional arguments only")
        kinds = FUNCTIONS[function][1]
        required = [kind for kind in kinds if not kind.endswith("?")]
        if not len(required) <= len(node.args) <= len(kinds):
            self._fail(f"{function}() takes {len(required)}{'-' + str(len(kinds)) if len(kinds) > len(required) else ''}"
                       f" arguments, got {len(node.args)}")
        for argument, kind in zip(node.args, kinds):
            if kind.startswith("periods"):
                if not isinstance(argument, ast.Constant):
                    self._fail(f"the period in {function}() must be a number of candles or a duration like '1d'")
                try:
                    parse_period(argument.value, self.granularity_seconds, f"{function}() period")
                except ValueError as exc:
                    self._fail(str(exc))
            else:
                self._check(argument, known)

    # --- evaluation -------------------------------------------------------------------

    def evaluate(self, series):
        """Return a boolean array: True where the rule is known to hold."""
        length = len(series["close"])
        value = np.broadcast_to(np.asarray(self._eval(self.tree.body, series), dtype=float), (length,))
        return _known_true(value)

    def _eval(self, node, series):
        name = self._series_name(node)
        if name in ("True", "False"):
            return 1.0 if name == "True" else 0.0
        if name is not None:
            return series[name]
        if isinstance(node, ast.Constant):
            return float(node.value)
        with np.errstate(divide="ignore", invalid="ignore"):
            if isinstance(node, ast.BoolOp):
                parts = [np.nan_to_num(self._eval(v, series), nan=0.0) == 1.0 for v in node.values]
                combine = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
                result = parts[0]
                for part in parts[1:]:
                    result = combine(result, part)
                return result.astype(float)
            if isinstance(node, ast.UnaryOp):
                operand = self._eval(node.operand, series)
                if isinstance(node.op, ast.Not):
                    return np.where(np.isnan(operand), np.nan, 1.0 - operand)
                return -operand if isinstance(node.op, ast.USub) else operand
            if isinstance(node, ast.BinOp):
                result = np.asarray(ARITHMETIC[type(node.op)](self._eval(node.left, series),
                                                              self._eval(node.right, series)), dtype=float)
                # Division by zero is "unknown", not infinity, so it can never satisfy a rule.
                return np.where(np.isfinite(result), result, np.nan)
            if isinstance(node, ast.Compare):
                # Chained comparisons like "30 < rsi < 70" mean each adjacent pair holds.
                values = [np.asarray(self._eval(part, series), dtype=float)
                          for part in [node.left, *node.comparators]]
                result = None
                for op, left, right in zip(node.ops, values, values[1:]):
                    left, right = np.broadcast_arrays(left, right)
                    step = _condition(COMPARISONS[type(op)](left, right), left, right)
                    result = step if result is None else _and(result, step)
                return result
            return self._call(node, series)

    def _call(self, node, series):
        function = node.func.id
        kinds = FUNCTIONS[function][1]
        args = []
        for argument, kind in zip(node.args, kinds):
            if kind.startswith("periods"):
                args.append(parse_period(argument.value, self.granularity_seconds))
            else:
                args.append(np.asarray(self._eval(argument, series), dtype=float))
        if function in ("crosses_above", "crosses_below"):
            a, b = np.broadcast_arrays(*args)
            before_a, before_b = shift(a, 1), shift(b, 1)
            if function == "crosses_above":
                crossed = (a > b) & (before_a <= before_b)
            else:
                crossed = (a < b) & (before_a >= before_b)
            return _condition(crossed, a, b, before_a, before_b)
        x = args[0]
        periods = args[1] if len(args) > 1 else 1
        if function == "previous":
            return shift(x, periods)
        if function == "change":
            return x - shift(x, periods)
        if function == "pct_change":
            result = x / shift(x, periods) - 1
            return np.where(np.isfinite(result), result, np.nan)
        if function == "highest":
            return rolling_max(x, periods)
        if function == "lowest":
            return rolling_min(x, periods)
        if function == "abs":
            return np.abs(x)
        if function == "min":
            return np.minimum(*args)
        return np.maximum(*args)


def evaluate_rules(compiled, series, length):
    """True where every "all" rule holds and, if any are listed, at least one "any" rule."""
    result = np.ones(length, dtype=bool)
    for rule in compiled["all"]:
        result &= rule.evaluate(series)
    if compiled["any"]:
        any_result = np.zeros(length, dtype=bool)
        for rule in compiled["any"]:
            any_result |= rule.evaluate(series)
        result &= any_result
    return result
