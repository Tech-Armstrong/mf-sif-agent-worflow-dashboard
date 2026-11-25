# Container Apps Job "parse-portfolio": parses MF and SIF Monthly Portfolio Disclosures from the parse-portfolio queue.
# Build from the repo root:  docker build -f jobs/portfolio.Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PARSE_RUNNER=job

WORKDIR /app
COPY jobs/requirements-portfolio.txt jobs/
RUN pip install --no-cache-dir -r jobs/requirements-portfolio.txt
COPY pipeline/ pipeline/
COPY parsers/ parsers/

CMD ["python", "-m", "pipeline.worker", "portfolio"]
