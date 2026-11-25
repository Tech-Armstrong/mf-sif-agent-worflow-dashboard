# Container Apps Job "parse-factsheet": parses MF and SIF factsheets (PDF) from the parse-factsheet queue with docling
# and the Azure OpenAI extraction agent. Build from the repo root:  docker build -f jobs/factsheet.Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PARSE_RUNNER=job \
    HF_HOME=/opt/hf OMP_NUM_THREADS=4

# libGL / glib for the image libraries docling loads
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY jobs/requirements-portfolio.txt jobs/requirements-factsheet.txt jobs/
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r jobs/requirements-factsheet.txt
COPY pipeline/ pipeline/
COPY parsers/ parsers/
COPY jobs/warm_docling.py jobs/

# Download docling's models into the image (MF's and the SIF parser's settings): the parsers run with HF_HUB_OFFLINE=1
RUN HF_HUB_OFFLINE=0 python jobs/warm_docling.py \
    && HF_HUB_OFFLINE=0 python jobs/warm_docling.py parsers/sif/factsheets

CMD ["python", "-m", "pipeline.worker", "factsheet"]
