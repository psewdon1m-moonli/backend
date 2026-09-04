FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

COPY pyproject.toml /app/pyproject.toml
COPY requirements.lock /app/requirements.lock
COPY README.md /app/README.md
COPY app /app/app
COPY config /app/config
COPY integrations /app/integrations

RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock && \
    useradd --create-home --uid 10001 moonli && \
    mkdir -p /app/data /app/secrets && \
    chown -R moonli:moonli /app/data /app/secrets

USER moonli

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
