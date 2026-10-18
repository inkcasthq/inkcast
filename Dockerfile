# syntax=docker/dockerfile:1
# Build: docker build -t inkcast .   (CI pins the base images by digest; see .github/workflows/ci.yml)

FROM python:3.14-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.14-slim
LABEL org.opencontainers.image.title="inkcast" \
      org.opencontainers.image.description="Publish and schedule writing across platforms from any MCP client" \
      org.opencontainers.image.source="https://github.com/inkcasthq/inkcast" \
      org.opencontainers.image.licenses="LicenseRef-PolyForm-Shield-1.0.0" \
      org.opencontainers.image.vendor="ScalarOps Technologies LLC"
# INKCAST_ENV=production: the posture checks are strict by default; a lab overlay sets ci/development.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 PORT=8787 INKCAST_METRICS_PORT=9090 INKCAST_LOG_FORMAT=json INKCAST_ENV=production \
    INKCAST_DATABASE=/data/inkcast.db HOME=/tmp
COPY --from=build /dist/*.whl /tmp/
COPY LICENSE NOTICE /licenses/
RUN pip install /tmp/*.whl && rm -f /tmp/*.whl \
 # OpenShift runs containers as an arbitrary UID in group 0: make writable paths group-0 owned.
 && mkdir -p /data && chgrp -R 0 /data && chmod -R g=u /data
USER 1001
VOLUME ["/data"]
EXPOSE 8787 9090
# No HEALTHCHECK: Kubernetes/OpenShift probes (/livez, /readyz) are the source of truth.
ENTRYPOINT ["inkcast"]
CMD ["all"]
