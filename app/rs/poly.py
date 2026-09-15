"""Polynomials over GF(256), coefficients stored in ascending order:
p[i] is the coefficient of x^i.
"""

from . import gf


def trim(p: list[int]) -> list[int]:
    """Drop high-degree zero coefficients in place (keeps at least one)."""
    while len(p) > 1 and p[-1] == 0:
        p.pop()
    return p


def mul(p: list[int], q: list[int]) -> list[int]:
    out = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a == 0:
            continue
        for j, b in enumerate(q):
            if b:
                out[i + j] ^= gf.mul(a, b)
    return out


def evaluate(p: list[int], x: int) -> int:
    """Horner evaluation of p at field element x."""
    acc = 0
    for coef in reversed(p):
        acc = gf.mul(acc, x) ^ coef
    return acc


def derivative(p: list[int]) -> list[int]:
    """Formal derivative. In characteristic 2, d/dx x^n = x^(n-1) for odd n
    and 0 for even n, so the odd-indexed coefficients of p land on the
    (even) index n-1 of the result."""
    out = [0] * max(len(p) - 1, 1)
    for i in range(1, len(p), 2):
        out[i - 1] = p[i]
    return out
