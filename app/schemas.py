"""Request/response models for the decoding and assembly endpoints."""

from pydantic import BaseModel, Field, field_validator, model_validator

from .rs.decoder import N

CODEWORD_HEX_LENGTH = 2 * N  # 255 bytes -> 510 hex characters

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _validate_hex(value: str, *, field_name: str) -> str:
    """Reject anything that is not an even-length run of hex digits.

    ``bytes.fromhex()`` silently skips ASCII whitespace, which would turn a
    right-length string into fewer bytes and fail later as a server error.
    """
    if len(value) % 2:
        raise ValueError(f"{field_name} must contain whole bytes (even hex length)")
    if not all(c in _HEX_DIGITS for c in value):
        raise ValueError(f"{field_name} must be hexadecimal")
    return value


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
        return _validate_hex(v, field_name="codeword")

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


# ---------------------------------------------------------------------------
# Fragment assembly
# ---------------------------------------------------------------------------


class FragmentRequest(BaseModel):
    offset: int = Field(
        ..., ge=0, lt=N, description="Frame-absolute byte offset of the fragment"
    )
    data: str = Field(
        ...,
        min_length=2,
        description="Fragment bytes as hexadecimal; must fit inside the 255-byte frame",
    )
    erasures: list[int] = Field(
        default_factory=list,
        description=(
            "0-based positions unreliable within this fragment; translated to "
            "frame positions using offset"
        ),
    )

    @field_validator("data")
    @classmethod
    def data_must_be_hex_bytes(cls, v: str) -> str:
        return _validate_hex(v, field_name="data")

    @field_validator("erasures")
    @classmethod
    def erasures_must_be_distinct(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate erasure positions within fragment")
        return v

    @model_validator(mode="after")
    def fragment_must_fit_in_frame(self):
        raw = bytes.fromhex(self.data)
        if self.offset + len(raw) > N:
            raise ValueError(
                f"fragment spans bytes [{self.offset}, {self.offset + len(raw)}) "
                f"beyond the {N}-byte frame"
            )
        n = len(raw)
        for pos in self.erasures:
            if pos < 0 or pos >= n:
                raise ValueError(
                    f"fragment erasure {pos} out of range [0, {n - 1}] "
                    f"for a {n}-byte fragment"
                )
        return self


class ByteRange(BaseModel):
    start: int = Field(..., ge=0, lt=N)
    end: int = Field(..., ge=0, lt=N, description="Inclusive")


class AssemblyProgress(BaseModel):
    status: str
    assembly_id: str
    received_bytes: int
    total_bytes: int = N
    complete: bool
    missing_ranges: list[ByteRange] = Field(
        default_factory=list,
        description="Canonical inclusive gaps (sorted, adjacent positions merged)",
    )
    erasures: list[int] = Field(
        default_factory=list, description="De-duplicated frame-absolute erasure claims"
    )
    # Present only when the assembly has been completed by a decode submit:
    # the exact body a POST /decode of the assembled frame would return.
    decode: dict | None = None


class AssemblyRejected(BaseModel):
    status: str = "rejected"
    assembly_id: str
    conflict_ranges: list[ByteRange] = Field(
        ...,
        description=(
            "Canonical inclusive ranges where overlapping bytes differ or "
            "erasure claims contradict reliable data"
        ),
    )
