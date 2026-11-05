# syntax=docker/dockerfile:1
# Build: docker build -t inkcast .   (base images pinned by tag and digest; scripts/images.py, Dependabot moves them)

FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS build
WORKDIR /src
# Hash-locked (E1.F1.8.S3): the build backend from build.lock, every file checked against its sha256, and no build
# isolation, so nothing unlocked is fetched while building the wheel.
COPY build.lock ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r build.lock
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /dist .

FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
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
COPY requirements.lock /tmp/
COPY LICENSE NOTICE /licenses/
# Exactly the locked runtime dependencies, each checked against its sha256; then the wheel itself without resolving
# anything; then pip check proves the lock satisfies the wheel's requirements.
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock \
 && pip install --no-deps /tmp/*.whl && pip check && rm -f /tmp/*.whl /tmp/requirements.lock \
 # OpenShift runs containers as an arbitrary UID in group 0: make writable paths group-0 owned.
 && mkdir -p /data && chgrp -R 0 /data && chmod -R g=u /data \
 # No package manager at run time: nothing here installs a package, and what isn't shipped can't be exploited or fail
 # the image scan (apt's CVE-2026-109518 failed it before the base image was rebuilt). Nothing installed depends on it.
 && dpkg --purge apt libapt-pkg7.0
USER 1001
VOLUME ["/data"]
EXPOSE 8787 9090
# No HEALTHCHECK: Kubernetes/OpenShift probes (/livez, /readyz) are the source of truth.
ENTRYPOINT ["inkcast"]
CMD ["all"]
