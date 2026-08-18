FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# The upstream dependency supplies the paper-search CLI used by the worker.
RUN pip install --no-cache-dir --disable-pip-version-check \
    "paper-search-mcp==0.1.4" \
    "pymongo>=4.9,<5"

COPY app ./app
COPY templates ./templates

RUN useradd --create-home --uid 10001 papers \
    && mkdir -p /data \
    && chown -R papers:papers /app /data

USER papers

EXPOSE 8099

CMD ["python", "-m", "app.pipeline", "--loop"]
