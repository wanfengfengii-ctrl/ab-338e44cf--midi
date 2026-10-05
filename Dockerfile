# syntax=docker/dockerfile:1

# --- test stage: used by the one-shot verify service ---------------------
FROM python:3.12-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

# --- runtime stage -------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000
WORKDIR /app
COPY app ./app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=5 \
    CMD ["python", "-m", "app.healthcheck"]
CMD ["python", "-m", "app.server"]
