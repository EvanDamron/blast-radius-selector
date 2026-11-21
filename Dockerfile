# Same base image the labels were made in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot source: the selector diffs each patch against it.
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<PY
import io, tarfile, urllib.request
url = "https://github.com/tobymao/sqlglot/archive/${SQLGLOT_COMMIT}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        tar.extract(m, "/opt/sqlglot", filter="data")
PY
RUN chmod -R a+rX /opt/sqlglot

COPY selector.py /app/selector.py
COPY map /app/map
RUN chmod -R a+rX /app && python -m compileall -q /app/selector.py
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
