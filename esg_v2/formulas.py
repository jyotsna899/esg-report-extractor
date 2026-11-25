"""
Safe evaluation of the KPI formulas in config/kpis.csv.

A formula is an arithmetic expression over datapoint ids (and earlier Helper
KPIs), e.g. "renewable_energy / total_energy * 100". Alternatives are
separated by "||": the first alternative whose inputs are all available wins.
Only + - * / and parentheses are allowed - no Python code is executed.
"""

import ast
import operator

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class MissingInput(Exception):
    def __init__(self, name):
        super().__init__(name)
        self.name = name


def names_in(expr: str) -> list[str]:
    tree = ast.parse(expr, mode="eval")
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.append(node.id)
    return seen


def evaluate(expr: str, env: dict):
    """Evaluates one expression. Raises MissingInput if a name has no value."""
    tree = ast.parse(expr, mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            v = env.get(node.id)
            if v is None:
                raise MissingInput(node.id)
            return v
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ZeroDivisionError(expr)
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError(f"Unsupported syntax in formula: {expr!r}")

    return _eval(tree)


def evaluate_with_alternatives(formula: str, env: dict):
    """
    Returns (value, expression_used, names_used) or (None, None, missing_names).
    """
    missing: list[str] = []
    for alt in [a.strip() for a in formula.split("||") if a.strip()]:
        try:
            return evaluate(alt, env), alt, names_in(alt)
        except MissingInput as m:
            if m.name not in missing:
                missing.append(m.name)
        except ZeroDivisionError:
            missing.append(f"zero denominator in '{alt}'")
    return None, None, missing
