"""RS(255, 223) errors-and-erasures decoder over GF(256), implemented from
first principles (Berlekamp-Massey + Chien search + Forney's formula).

Conventions
-----------
* A frame is a list of 255 bytes; byte i is the coefficient of x^(254-i)
  in the codeword polynomial.
* GF(256) is defined by 0x11d with alpha = 0x02 (see gf.py).
* The generator polynomial has roots alpha^0 .. alpha^31.
* Syndromes are S_j = r(alpha^j) for j = 0..31.

Decoding succeeds iff the unique codeword within the bounded-distance
radius exists: with e unknown errors and s erasures, 2e + s <= 32. The
minimum distance is 33, so such a codeword is necessarily unique.
"""

from . import gf
from .poly import derivative, evaluate, mul as poly_mul, trim

N = 255
K = 223
NSYM = N - K  # 32 parity symbols, minimum distance 33


class UncorrectableError(Exception):
    """Raised when no unique codeword satisfies 2e + s <= NSYM.

    Carries the 32 syndromes of the received frame so the caller can
    report them for independent recomputation.
    """

    def __init__(self, syndromes: list[int]):
        super().__init__("no unique codeword within the 2e + s <= 32 radius")
        self.syndromes = list(syndromes)


def compute_syndromes(word) -> list[int]:
    """S_j = r(alpha^j) for j = 0..31, where word[i] is the coefficient of
    x^(254-i). Horner over the bytes in frame order evaluates exactly this."""
    word = list(word)
    if len(word) != N:
        raise ValueError(f"codeword must be {N} bytes")
    syndromes = []
    for j in range(NSYM):
        x = gf.EXP[j]
        acc = 0
        for byte in word:
            acc = gf.mul(acc, x) ^ byte
        syndromes.append(acc)
    return syndromes


def _locator_x(position: int) -> int:
    """Field element X such that an error of magnitude Y at byte `position`
    contributes Y * X^j to syndrome S_j: X = alpha^(254 - position)."""
    return gf.EXP[(N - 1 - position) % gf.MULTIPLICATIVE_ORDER]


def _locator_x_inv(position: int) -> int:
    """X^-1 for the position locator: alpha^(position - 254)."""
    return gf.EXP[(position + 1) % gf.MULTIPLICATIVE_ORDER]


def _berlekamp_massey(sequence: list[int]) -> list[int]:
    """Connection polynomial C (C[0] = 1) of the shortest LFSR that
    generates `sequence`: sum_i C[i] * sequence[n - i] == 0 for n >= deg C."""
    C = [1]
    B = [1]
    L = 0
    m = 1
    b = 1
    for n in range(len(sequence)):
        d = 0
        for i in range(L + 1):
            d ^= gf.mul(C[i], sequence[n - i])
        if d == 0:
            m += 1
            continue
        T = C[:]
        coef = gf.div(d, b)
        if len(C) < len(B) + m:
            C.extend([0] * (len(B) + m - len(C)))
        for i in range(len(B)):
            C[i + m] ^= gf.mul(coef, B[i])
        if 2 * L <= n:
            L = n + 1 - L
            B = T
            b = d
            m = 1
        else:
            m += 1
    return C


def decode(word, erasures=()):
    """Decode a 255-byte frame with erasures at the given 0-based positions.

    Returns (corrected_word, corrected_positions) where corrected_positions
    lists, in ascending order, every byte the decoder changed. Raises
    UncorrectableError when no unique codeword satisfies 2e + s <= 32.
    """
    word = list(word)
    if len(word) != N:
        raise ValueError(f"codeword must be {N} bytes")
    erasures = sorted(erasures)
    if len(set(erasures)) != len(erasures):
        raise ValueError("duplicate erasure positions")
    if any(p < 0 or p >= N for p in erasures):
        raise ValueError("erasure position out of range")

    syndromes = compute_syndromes(word)
    s = len(erasures)
    if s > NSYM:
        # More erasures than parity symbols: the payload is ambiguous.
        raise UncorrectableError(syndromes)
    if not any(syndromes):
        # Already a valid codeword: nothing to correct, report no changes.
        return word, []

    # Erasure locator Gamma(x) = prod_k (1 + X_k x) over the erasures.
    gamma = [1]
    for pos in erasures:
        gamma = poly_mul(gamma, [1, _locator_x(pos)])

    # Forney syndromes: coefficients of x^s .. x^31 of S(x) * Gamma(x);
    # this removes the erasure contribution so BM sees only the errors.
    t = poly_mul(syndromes, gamma)
    forney = [t[i] if i < len(t) else 0 for i in range(s, NSYM)]

    lam = trim(_berlekamp_massey(forney))
    e = len(lam) - 1
    if 2 * e + s > NSYM:
        raise UncorrectableError(syndromes)

    # Combined locator Lambda(x) = Lambda_errors(x) * Gamma(x).
    locator = poly_mul(lam, gamma)

    # Chien search: byte position i is a root iff Lambda(X_i^-1) == 0.
    positions = [i for i in range(N) if evaluate(locator, _locator_x_inv(i)) == 0]
    if len(positions) != len(locator) - 1:
        raise UncorrectableError(syndromes)

    # Error evaluator Omega(x) = S(x) * Lambda(x) mod x^32.
    omega = poly_mul(syndromes, locator)[:NSYM]
    locator_deriv = derivative(locator)

    corrected = list(word)
    for i in positions:
        x = _locator_x(i)
        x_inv = _locator_x_inv(i)
        denominator = evaluate(locator_deriv, x_inv)
        if denominator == 0:
            raise UncorrectableError(syndromes)
        # Forney's formula (characteristic 2): Y = X * Omega(X^-1) / Lambda'(X^-1).
        magnitude = gf.div(gf.mul(x, evaluate(omega, x_inv)), denominator)
        corrected[i] ^= magnitude

    if any(compute_syndromes(corrected)):
        raise UncorrectableError(syndromes)

    changed = [i for i in positions if corrected[i] != word[i]]
    return corrected, changed
