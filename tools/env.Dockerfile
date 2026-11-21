# Test environment matching the labelers': same base image and package lock.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
ENV SKIP_INTEGRATION=0 PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
