FROM python:3.11-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 xstorybot \
    && useradd --system --uid 10001 --gid xstorybot --home-dir /app xstorybot

# provider ごとの依存だけを入れる。任意 plugin の依存は XSBOT_EXTRA_REQUIREMENTS で追加する
ARG XSBOT_CLOUD_PROVIDER=gcp
ARG XSBOT_EXTRA_REQUIREMENTS=
COPY requirements*.txt ./
RUN case "$XSBOT_CLOUD_PROVIDER" in gcp|aws) ;; *) echo "XSBOT_CLOUD_PROVIDER は gcp か aws を指定してください" >&2; exit 1 ;; esac \
    && pip install --no-cache-dir -r "requirements-${XSBOT_CLOUD_PROVIDER}.txt" \
    && if [ -n "$XSBOT_EXTRA_REQUIREMENTS" ]; then pip install --no-cache-dir -r "$XSBOT_EXTRA_REQUIREMENTS"; fi
ENV XSBOT_CLOUD_PROVIDER=${XSBOT_CLOUD_PROVIDER}

COPY --chown=xstorybot:xstorybot . ./
RUN test -s settings.yaml || { echo "settings.yamlを作成してからビルドしてください" >&2; exit 1; }

USER xstorybot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/healthz', timeout=2)"

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers ${GUNICORN_WORKERS:-1} --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-0} --access-logfile - --error-logfile - ${XSBOT_APP_MODULE:-app:app}"]
