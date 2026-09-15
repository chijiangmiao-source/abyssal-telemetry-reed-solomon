"""FastAPI application exposing the RS(255, 223) errors-and-erasures decoder,
both as a one-shot /decode call and as out-of-order fragment assembly."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .assembly import (
    COMPLETED,
    GAPS,
    REJECTED,
    RESULT,
    AssemblyStore,
)
from .rs.decoder import K, UncorrectableError, decode
from .schemas import (
    AssemblyProgress,
    AssemblyRejected,
    DecodeRequest,
    DecodeSuccess,
    FragmentRequest,
    UncorrectableResponse,
)

app = FastAPI(
    title="RS(255,223) acoustic-frame decoder",
    version="1.1.0",
    description=(
        "Bounded-distance errors-and-erasures decoding of 255-byte frames. "
        "Succeeds iff a unique codeword satisfies 2e + s <= 32. Fragments may "
        "be appended out of order under /assemblies/{id}."
    ),
)

# Process-local sessions only; a restart starts every id over from scratch.
assemblies = AssemblyStore()


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


def decode_body(word, erasures):
    """Map (word, erasures) to the exact 200/422 /decode body.

    Single source of truth shared by POST /decode and assembly submits.
    Returns ``(http_status, body_dict)``.
    """
    try:
        corrected, positions = decode(list(word), erasures)
    except UncorrectableError as exc:
        return 422, UncorrectableResponse(
            syndromes=[f"{s:02x}" for s in exc.syndromes]
        ).model_dump()
    return 200, DecodeSuccess(
        payload=bytes(corrected[:K]).hex(),
        corrected_codeword=bytes(corrected).hex(),
        corrected_positions=positions,
    ).model_dump()


def result_body(result):
    """Render the session's cached decoder tuple as the /decode body."""
    if result[0] == "uncorrectable":
        return UncorrectableResponse(
            syndromes=[f"{s:02x}" for s in result[1]]
        ).model_dump()
    _, corrected, positions = result
    return DecodeSuccess(
        payload=bytes(corrected[:K]).hex(),
        corrected_codeword=bytes(corrected).hex(),
        corrected_positions=positions,
    ).model_dump()


def progress_response(view):
    """Build the 200/409 progress envelope from a session view."""
    body = AssemblyProgress(
        status=view["status"],
        assembly_id=view["id"],
        received_bytes=view["received_bytes"],
        total_bytes=view["total_bytes"],
        complete=view["complete"],
        missing_ranges=view["missing_ranges"],
        erasures=view["erasures"],
        decode=result_body(view["result"]) if view["result"] else None,
    )
    # A completed view carries the terminal decoder result; a gapped submit
    # is a 409, ordinary progress a 200 -- the caller sets the status.
    return body


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/decode", response_model=DecodeSuccess)
def decode_frame(request: DecodeRequest):
    status, body = decode_body(bytes.fromhex(request.codeword), request.erasures)
    return JSONResponse(status_code=status, content=body)


@app.put("/assemblies/{assembly_id}/fragments", response_model=AssemblyProgress)
def put_fragment(assembly_id: str, fragment: FragmentRequest):
    """Append (or retry) one fragment; safe to call out of order.

    Same-content retries while collecting just return current progress.
    Contradictory overlaps atomically reject the whole session (409). After
    completion, late fragments replay the immutable terminal result.
    """
    session = assemblies.session(assembly_id)
    data = bytes.fromhex(fragment.data)
    # Erasure indices are fragment-local; the session works frame-absolute.
    erasures = [fragment.offset + pos for pos in fragment.erasures]

    tag, payload = session.append(fragment.offset, data, erasures)
    if tag == REJECTED:
        return JSONResponse(
            status_code=409,
            content=AssemblyRejected(
                assembly_id=assembly_id, conflict_ranges=payload
            ).model_dump(),
        )
    if tag == COMPLETED:
        # Late fragment: replay the immutable terminal, including the
        # original 200/422 decoder status.
        http_status = 200 if payload["result"][0] == "ok" else 422
        return JSONResponse(
            status_code=http_status, content=progress_response(payload).model_dump()
        )
    return progress_response(payload)


@app.post("/assemblies/{assembly_id}/decode", response_model=AssemblyProgress)
def submit_assembly(assembly_id: str):
    """Submit the assembled frame for decoding.

    Gaps -> 409 with normalized missing ranges; the first complete submit
    caches the 200/422 decoder result, which all later submits replay. A
    rejected session replays its 409 conflict ranges.
    """
    session = assemblies.session(assembly_id)
    tag, payload = session.submit()
    if tag == REJECTED:
        return JSONResponse(
            status_code=409,
            content=AssemblyRejected(
                assembly_id=assembly_id, conflict_ranges=payload
            ).model_dump(),
        )
    if tag == GAPS:
        return JSONResponse(
            status_code=409, content=progress_response(payload).model_dump()
        )
    # RESULT: the one cached decoder outcome, replayed forever with the
    # exact 200/422 semantics POST /decode would have produced.
    http_status = 200 if payload["result"][0] == "ok" else 422
    return JSONResponse(
        status_code=http_status, content=progress_response(payload).model_dump()
    )
