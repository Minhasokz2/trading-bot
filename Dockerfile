# Coin Audit web app — one container: the audit engine + the password-protected web UI + the scheduler.
# Build:  docker build -t coin-audit .
# Run:    docker run -p 10000:10000 -e COIN_AUDIT_PASSWORD=choose-one -v coinaudit:/data coin-audit
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    COIN_AUDIT_DATA_DIR=/data \
    COIN_AUDIT_TRUST_PROXY=1

# libgomp1: the OpenMP runtime LightGBM needs.  gosu: drop root after fixing the ownership of the mounted disk.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 gosu \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown app:app /data

WORKDIR /app
COPY requirements-core.txt requirements-web.txt requirements-web-lock.txt ./
RUN pip install -r requirements-web.txt -c requirements-web-lock.txt

COPY audit ./audit
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 10000
ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "audit/serve.py"]
