"""GF(2^8) arithmetic for the RS(255, 223) codec.

The field is GF(2)[x] / (p(x)) with p(x) = x^8 + x^4 + x^3 + x + 1, written
as the byte polynomial 0x11d. The primitive element alpha is the polynomial
x, i.e. the byte 0x02.
"""

PRIMITIVE_POLYNOMIAL = 0x11D
FIELD_SIZE = 256
MULTIPLICATIVE_ORDER = FIELD_SIZE - 1  # 255, order of the multiplicative group
ALPHA = 0x02

# EXP[i] = alpha^i for i in [0, 2*255); the doubled span avoids the modulo
# operation in multiplication. LOG[a] = i such that alpha^i = a (a != 0).
EXP = [0] * (2 * MULTIPLICATIVE_ORDER)
LOG = [0] * FIELD_SIZE


def _build_tables() -> None:
    x = 1
    for i in range(MULTIPLICATIVE_ORDER):
        EXP[i] = x
        LOG[x] = i
        x <<= 1
        if x & FIELD_SIZE:
            x ^= PRIMITIVE_POLYNOMIAL
    for i in range(MULTIPLICATIVE_ORDER, 2 * MULTIPLICATIVE_ORDER):
        EXP[i] = EXP[i - MULTIPLICATIVE_ORDER]


_build_tables()


def add(a: int, b: int) -> int:
    return a ^ b


def sub(a: int, b: int) -> int:
    return a ^ b


def mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(256)")
    if a == 0:
        return 0
    return EXP[(LOG[a] - LOG[b]) % MULTIPLICATIVE_ORDER]


def pow(a: int, n: int) -> int:
    if n < 0:
        raise ValueError("negative exponent")
    if a == 0:
        return 1 if n == 0 else 0
    return EXP[(LOG[a] * n) % MULTIPLICATIVE_ORDER]


def inverse(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("inverse of zero in GF(256)")
    return EXP[MULTIPLICATIVE_ORDER - LOG[a]]
