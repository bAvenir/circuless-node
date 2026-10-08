# The reference service as an image.
#
# Deliberately plain: a partner should be able to read this in one pass and recognise
# their own stack in it. Pinned base and pinned dependencies because that is the house
# rule (C16) and because an example that drifts is an example that stops working.
FROM docker.io/library/python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN pip install --no-cache-dir "fastapi==0.120.4" "uvicorn[standard]==0.38.0"

# Non-root. Nothing here needs to write anything, so the container also runs with a
# read-only root filesystem — see docker-compose.yml.
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin service
COPY --chown=service:service service.py identity.py minimal.py /srv/
USER service
WORKDIR /srv

EXPOSE 8080

# No key here, and none baked into the image. It arrives as an environment variable at
# run time, and the node holds the only other copy — encrypted (N10).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

# Tier 0 by default — the service that knows nothing about CIRCULess. Override with
# SERVICE_MODULE=identity for the identity-aware version; the compose file does it.
ENV SERVICE_MODULE=service
CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE}:app --host 0.0.0.0 --port 8080 --no-server-header"]
