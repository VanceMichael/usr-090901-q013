# Embodied-intelligence lab scheduling service - runtime image.
# Pure Python 3.12 standard library: no third-party runtime packages.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    RULES_PATH=fixtures/rules.json

WORKDIR /app

# Copy only what the service needs (tests and tooling stay out of the image).
COPY app/ ./app/
COPY fixtures/ ./fixtures/
COPY contracts/ ./contracts/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Container-level liveness probe (Compose healthcheck mirrors this).
HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()" \
    || exit 1

ENTRYPOINT ["python", "-m", "app"]
