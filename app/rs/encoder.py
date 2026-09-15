"""Systematic RS(255, 223) encoder.

Used by the test-suite and the acceptance service to produce valid frames;
the decoder never relies on it. Conventions match the decoder: byte i of
the codeword is the coefficient of x^(254-i), the generator polynomial has
roots alpha^0 .. alpha^31.
"""

from . import gf
from .poly import mul as poly_mul

N = 255
K = 223
NSYM = N - K


def generator_polynomial() -> list[int]:
    """g(x) = prod_{j=0}^{31} (x - alpha^j); in characteristic 2, -a = a."""
    g = [1]
    for j in range(NSYM):
        g = poly_mul(g, [gf.EXP[j], 1])
    return g


GENERATOR = generator_polynomial()


def encode(payload: bytes) -> bytes:
    """Systematic encoding: the first 223 codeword bytes equal the payload,
    the remaining 32 bytes are the parity of payload * x^32 modulo g(x)."""
    if len(payload) != K:
        raise ValueError(f"payload must be exactly {K} bytes")
    # work[i] is the coefficient of x^(254-i): payload followed by 32 zeros.
    work = list(payload) + [0] * NSYM
    gen_desc = GENERATOR[::-1]  # leading coefficient first (it is 1)
    for i in range(K):
        coef = work[i]
        if coef == 0:
            continue
        for j in range(1, NSYM + 1):
            work[i + j] ^= gf.mul(coef, gen_desc[j])
    remainder = work[K:]
    return bytes(list(payload) + remainder)
