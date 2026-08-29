FROM python:3.11-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 xstorybot \
    && useradd --system --uid 10001 --gid xstorybot --home-dir /app xstorybot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=xstorybot:xstorybot . ./
RUN cp settings.yaml.template settings.yaml \
    && chown xstorybot:xstorybot settings.yaml

USER xstorybot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/healthz', timeout=2)"

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers ${GUNICORN_WORKERS:-1} --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-0} --access-logfile - --error-logfile - ${XSBOT_APP_MODULE:-app:app}"]
