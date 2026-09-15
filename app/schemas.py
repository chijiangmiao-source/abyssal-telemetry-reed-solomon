"""Request/response models for the decoding endpoint."""

from pydantic import BaseModel, Field, field_validator

from .rs.decoder import N

CODEWORD_HEX_LENGTH = 2 * N  # 255 bytes -> 510 hex characters

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class DecodeRequest(BaseModel):
    codeword: str = Field(
        ..., description="255-byte received frame as 510 hexadecimal characters"
    )
    erasures: list[int] = Field(
        default_factory=list,
        description="Distinct 0-based byte positions known to be unreliable",
    )

    @field_validator("codeword")
    @classmethod
    def codeword_must_be_hex(cls, v: str) -> str:
        if len(v) != CODEWORD_HEX_LENGTH:
            raise ValueError(
                f"codeword must be {N} bytes ({CODEWORD_HEX_LENGTH} hex characters)"
            )
        # bytes.fromhex() silently skips ASCII whitespace, so check every
        # character explicitly: a 510-char string with two spaces would
        # otherwise decode to 254 bytes and fail later as a server error.
        if not all(c in _HEX_DIGITS for c in v):
            raise ValueError("codeword must be hexadecimal")
        return v

    @field_validator("erasures")
    @classmethod
    def erasures_must_be_distinct_and_in_range(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate erasure positions")
        for pos in v:
            if pos < 0 or pos >= N:
                raise ValueError(f"erasure position {pos} out of range [0, {N - 1}]")
        return v


class DecodeSuccess(BaseModel):
    status: str = "ok"
    payload: str = Field(..., description="223-byte systematic payload, hex")
    corrected_codeword: str = Field(..., description="Repaired 255-byte frame, hex")
    corrected_positions: list[int] = Field(
        ..., description="Byte positions the decoder changed, ascending"
    )


class UncorrectableResponse(BaseModel):
    status: str = "uncorrectable"
    syndromes: list[str] = Field(
        ..., description="The 32 syndromes S_0..S_31 of the received frame, hex"
    )
