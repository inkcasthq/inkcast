# Dev only: the Inkcast image plus the test suite's fake DEV and Medium APIs (tests/fakes.py), for the kind
# stack in deploy/dev. Never published. Build from the repository root:
#   docker build -f deploy/dev/fakes.Dockerfile -t inkcast-fakes:dev --build-arg BASE=inkcast:ci .
ARG BASE=inkcast:ci
FROM ${BASE}
COPY tests/fakes.py /repo/tests/fakes.py
COPY scripts/dev/fake_platforms.py /repo/scripts/dev/fake_platforms.py
ENTRYPOINT ["python", "/repo/scripts/dev/fake_platforms.py"]
CMD []
