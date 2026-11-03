# Streamlit UI with tesseract for scanned pages.
#   docker build -t recon .
#   docker run --rm -p 8501:8501 --env-file .env -v "$PWD/output:/app/output" recon
# A batch from the command line instead of the UI:
#   docker run --rm -v "$PWD:/batch" recon recon run --pdf /batch/x.pdf --erp /batch/y.xlsx --out /batch/output
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md app.py run_batch.py ./
COPY src/ src/
COPY config/ config/
COPY data/ data/
# editable: config/ and app.py are found next to src/, and `recon` goes on PATH
RUN pip install --no-cache-dir --no-deps -e .

RUN useradd --create-home recon && mkdir -p output && chown recon output
USER recon

ENV PYTHONUNBUFFERED=1
EXPOSE 8501
CMD ["recon", "ui", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
