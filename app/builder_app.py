"""FastAPI instance #1: build the block-free handoff package.

The /build endpoint parses the request as a streaming, schema-driven JSON
document so that a legal request covering hundreds of thousands of street
blocks fits inside a 192 MiB process address space: no full-body buffer and
no dense block-by-district matrix are ever materialised.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .builder_core import BuilderConstraintError, build_from_coverage
from .models import BuildRequest, ErrorResponse, HandoffPackage
from .streaming import StreamParseError, StreamingBuildParser

# Hard caps matching the service contract; also bound the parser's tables.
MAX_OLD_DISTRICTS = 240
MAX_NEW_DISTRICTS = 240

app = FastAPI(
    title="District Re-zoning Package Builder",
    description=(
        "Submit old/new district codes together with the street-block IDs "
        "each district covers (a block may belong to several districts). "
        "Returns a JSON handoff package containing only district codes and "
        "the exact algebraic relations between their populations — no block "
        "IDs or per-block memberships are retained. The request is parsed "
        "incrementally, so very large manifests stay within the memory limit."
    ),
    version="1.1.0",
)


@app.exception_handler(StreamParseError)
def _stream_parse_error_handler(_request, exc: StreamParseError):
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error="invalid_json", detail=str(exc)
        ).model_dump(),
    )


@app.exception_handler(BuilderConstraintError)
def _constraint_error_handler(_request, exc: BuilderConstraintError):
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="invalid_request", detail=str(exc)
        ).model_dump(),
    )


@app.exception_handler(MemoryError)
def _memory_error_handler(_request, _exc: MemoryError):
    return JSONResponse(
        status_code=507,
        content=ErrorResponse(
            error="insufficient_memory",
            detail="the manifest exceeded the available process memory",
        ).model_dump(),
    )


@app.get("/", tags=["meta"])
def root():
    return {"service": "package-builder", "docs": "/docs", "endpoint": "/build"}


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post(
    "/build",
    response_model=HandoffPackage,
    responses={
        400: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    tags=["build"],
    summary="Build the JSON handoff package (streaming, memory-bounded)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/BuildRequest"}
                }
            },
        }
    },
)
async def build(request: Request) -> HandoffPackage:
    parser = StreamingBuildParser(MAX_OLD_DISTRICTS, MAX_NEW_DISTRICTS)
    # Small chunks keep the rolling text buffer tiny; the accumulator is the
    # only structure that grows with the number of blocks.
    async for chunk in request.stream():
        parser.feed(chunk)
    accumulator = parser.finish()
    package = build_from_coverage(
        parser.old_codes, parser.new_codes, accumulator
    )
    return HandoffPackage(**package)


def _custom_openapi():
    """Attach the BuildRequest schema so /docs still offers a body editor."""
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    # BuildRequest.model_json_schema() embeds its nested models under $defs.
    request_schema = BuildRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    components.update(request_schema.pop("$defs", {}))
    components["BuildRequest"] = request_schema
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi
