FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && useradd --uid 10001 --create-home inkcast \
 && mkdir /data && chown inkcast /data
USER 10001
ENV HOST=0.0.0.0 PORT=8787 INKCAST_DATABASE=/data/inkcast.db
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["inkcast"]
