FROM python:3.11-slim AS base

WORKDIR /app

# Install runtime dependencies only (layer caching)
FROM base AS deps
RUN pip install --no-cache-dir \
    prometheus_client==0.21.0 \
    flask==3.0.3 \
    manim==1.1.0 \
    numpy==1.26.4

# Production image
FROM base
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY . .

# Expose metrics port
EXPOSE 9090

# Health check
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/health')" || exit 1

# Default: run engine with metrics server, block on Manim render
# Override CMD per environment:
#   docker run cascade-sim --engine-only
#   docker run cascade-sim --metrics-only
ENTRYPOINT ["python", "run_v2.py"]

# Default args: run full pipeline (engine + metrics + render)
CMD ["--config", "configs/recovery_test.json", "--output-dir", "metrics"]