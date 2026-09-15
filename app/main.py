"""FastAPI application exposing the RS(255, 223) errors-and-erasures decoder."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .rs.decoder import K, UncorrectableError, decode
from .schemas import DecodeRequest, DecodeSuccess, UncorrectableResponse

app = FastAPI(
    title="RS(255,223) acoustic-frame decoder",
    version="1.0.0",
    description=(
        "Bounded-distance errors-and-erasures decoding of 255-byte frames. "
        "Succeeds iff a unique codeword satisfies 2e + s <= 32."
    ),
)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    # Malformed frames and out-of-range/duplicate erasures are client errors:
    # report them as 400 so 422 stays reserved for "uncorrectable".
    detail = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=400, content={"detail": detail})


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/decode", response_model=DecodeSuccess)
def decode_frame(request: DecodeRequest):
    word = list(bytes.fromhex(request.codeword))
    try:
        corrected, positions = decode(word, request.erasures)
    except UncorrectableError as exc:
        body = UncorrectableResponse(
            syndromes=[f"{s:02x}" for s in exc.syndromes]
        )
        return JSONResponse(status_code=422, content=body.model_dump())
    return DecodeSuccess(
        payload=bytes(corrected[:K]).hex(),
        corrected_codeword=bytes(corrected).hex(),
        corrected_positions=positions,
    )
