"""A CSV service. It knows nothing about CIRCULess.

Read it looking for the integration and you will not find one. That is the point: this
is **tier 0** — an ordinary HTTP API with an API key, the kind of thing a partner
already runs. Put it behind a node and it works, unmodified.

The node authenticates the caller, checks the agreement, writes the access log and then
calls this service with the credential it holds for it. To this service every caller
looks the same, because deciding *which* callers are allowed is not its job.

`identity.py` is tier 1: about forty lines that add per-user behaviour, for a service
that wants it. The difference between the two files is the entire cost of integrating.

    REFERENCE_SERVICE_KEY=$(openssl rand -hex 24) uvicorn service:app --port 8080
"""

from __future__ import annotations

import csv
import io
import os
import time
import uuid

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

#: The API key. An existing service almost certainly has something like this already —
#: and if it does, that is the whole integration: give the node the same value and
#: register the resource with `{"scheme": "header", "header_name": "X-API-Key"}`.
API_KEY = os.environ.get("REFERENCE_SERVICE_KEY", "")

#: How long a job takes. Tests set it to 0.
JOB_DELAY_S = float(os.environ.get("REFERENCE_SERVICE_JOB_DELAY_S", "2"))

app = FastAPI(title="CSV service", docs_url=None, redoc_url=None)

#: In memory, because this is an example. The point is that the **node** holds no job
#: state: it passes the 202 through and rewrites the Location so the consumer polls back
#: through it, which keeps every poll decided and logged.
JOBS: dict[str, dict] = {}


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


@app.exception_handler(Refused)
async def _refused(_request: Request, exc: Refused) -> JSONResponse:
    # The node passes an upstream's error body through unchanged on /invoke, so whatever
    # a service says here is what the caller reads. Say something useful.
    return JSONResponse(status_code=exc.status, content={"error": exc.detail})


def require_key(x_api_key: str = Header(default="")) -> None:
    """The only thing standing between this service and the internet.

    Nothing about CIRCULess. The node holds this key — encrypted, set by an
    organisation's admins and returned by no API — and sends it on every call. A service
    with its own existing key scheme changes nothing here; it just hands that key over.

    Fails closed when unset: a service that starts without its key and accepts
    everything is worse than one that refuses to start.
    """
    if not API_KEY:
        raise Refused(500, "this service has no REFERENCE_SERVICE_KEY configured")
    if x_api_key != API_KEY:
        raise Refused(401, "bad or missing X-API-Key")


def column_names(body: bytes) -> list[str]:
    if not body.strip():
        raise Refused(422, "the body is empty; send a CSV")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(422, "the body is not UTF-8") from None
    header = next(csv.reader(io.StringIO(text)), None)
    if not header or not any(name.strip() for name in header):
        raise Refused(422, "no header row")
    return [name.strip() for name in header]


@app.post("/headers", dependencies=[Depends(require_key)])
async def headers(request: Request) -> dict:
    """The CSV arrives in the body. This service never fetches anything.

    Not a restriction imposed on it — there is nothing it *could* fetch. It holds no
    credentials for the node, and the caller's token never reaches it. The caller
    downloads from the node, where that download is decided and logged against them,
    and posts the bytes here.
    """
    names = column_names(await request.body())
    return {"columns": names, "count": len(names)}


@app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
async def start_job(request: Request, response: Response) -> dict:
    """Accept the work and say where to look.

    `Location` is **relative, with no leading slash** — `jobs/<id>`. The node resolves
    it against the registered `endpoint_url` and refuses anything landing outside. A
    service registered at `https://host/api` answering `/jobs/7` is pointing at
    `https://host/jobs/7`, outside what was registered, and gets a 502. A relative path
    is correct under any registration.

    `Retry-After` reaches the caller. Each poll is a separate decision and a separate
    access-log entry on the node, so a client left to its own devices at 200 ms writes
    three hundred entries a minute. This header is how a service prevents that.
    """
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"started": time.monotonic(), "body": await request.body()}
    response.headers["Location"] = f"jobs/{job_id}"
    response.headers["Retry-After"] = str(max(1, int(JOB_DELAY_S)))
    return {"job": job_id, "status": "accepted"}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
async def poll_job(job_id: str, response: Response) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise Refused(404, "no such job")
    if time.monotonic() - job["started"] < JOB_DELAY_S:
        response.status_code = 202
        return {"job": job_id, "status": "running"}
    names = column_names(job["body"])
    return {"job": job_id, "status": "done", "columns": names, "count": len(names)}


@app.get("/redirect/inside", status_code=303, dependencies=[Depends(require_key)])
async def redirect_inside(response: Response) -> dict:
    """A redirect within this service. The node rewrites it to its own `/invoke` URL so
    the caller follows it back through the node rather than around it."""
    response.headers["Location"] = "headers"
    return {"see": "headers"}


@app.get("/redirect/outside", status_code=302, dependencies=[Depends(require_key)])
async def redirect_outside(response: Response) -> dict:
    """A redirect to somewhere else, which the node **refuses** with 502. Following it
    would take the caller out from behind the node — past the decision, past the
    agreement and past the log."""
    response.headers["Location"] = "https://example.invalid/elsewhere"
    return {"see": "somewhere else"}


@app.get("/healthz", include_in_schema=False)
async def healthz() -> Response:
    """No key required: it says nothing about the service beyond the process being up,
    which is what a container healthcheck needs."""
    return Response(status_code=200)
