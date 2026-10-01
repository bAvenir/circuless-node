# The node as an image (N15, O18).
#
# O18 asked whether partners install the node as a Python package or an image. This is
# the answer being an image, and the reason is the overlay: the NetBird agent runs as a
# sidecar sharing this container's network namespace, so the WireGuard interface exists
# only in here. Nothing else on the host can use it, and the node publishes no host port
# at all. A `uvx` install would put the agent on the host, and the whole machine on the
# overlay with it.
#
# `deploy/docker-compose.yml` is that arrangement; `deploy/README.md` is how a
# partner runs it.

# Pinned by digest (C16), so the thing we tested is the thing that runs and `cosign
# verify` has something fixed to verify. Resolve a new one with:
#   docker buildx imagetools inspect docker.io/library/python:3.12-slim-trixie
FROM docker.io/library/python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

# uv is pinned too: an unpinned build tool makes the lockfile's guarantee conditional on
# whatever was on PyPI that morning.
RUN pip install --no-cache-dir uv==0.10.6

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# `--frozen` fails rather than resolving: a build that quietly updated a dependency would
# ship something no test ever ran against.
# `--no-editable` installs the project properly, which is what makes
# `importlib.metadata.version("circuless-node")` — and so `/.well-known` and the
# heartbeat — report a real version.
RUN uv sync --frozen --no-dev --no-editable


FROM docker.io/library/python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime

# A fixed uid, not just a name. A partner bind-mounting a host directory has to chown it
# to something, and `10001` is a number the install guide can give them. Without it the
# node's own permission check — it refuses to start on a private key anyone else can
# read (invariant 17) — becomes the first thing a new install hits, with no clue why.
RUN groupadd --gid 10001 circuless \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin circuless \
 && mkdir -p /var/lib/circuless \
 && chown circuless:circuless /var/lib/circuless

COPY --from=builder --chown=circuless:circuless /app/.venv /app/.venv

# The schema, and the tool that applies it. Not optional and easy to leave out: the node
# starts and answers /healthz perfectly well without them, because liveness touches no
# database — and then fails on the first request that does. `alembic.ini` points at
# `%(here)s/migrations`, so the two travel together.
COPY --chown=circuless:circuless alembic.ini /app/alembic.ini
COPY --chown=circuless:circuless migrations /app/migrations

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CIRCULESS_NODE_DATA_DIR=/var/lib/circuless \
    CIRCULESS_NODE_DATABASE_URL=sqlite:////var/lib/circuless/node.db

# The database, the resource content, and the node's keypair and certificate all live
# here (identity.py writes the key beside the data). One volume, owned by the runtime
# user, is what keeps the 0600 check satisfiable.
VOLUME ["/var/lib/circuless"]

USER circuless
WORKDIR /var/lib/circuless

# Documentation only — with `network_mode: service:netbird` these are the namespace's
# ports, and nothing is published to the host.
EXPOSE 8000 8001

# The internal socket, which is loopback by default and shared with the sidecar through
# the namespace. Deliberately not the public one: a healthcheck that passed only because
# the gateway-facing socket answered would say nothing about the node being able to read
# its own database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=4)"]

ENTRYPOINT ["circuless-node"]
CMD ["serve"]
