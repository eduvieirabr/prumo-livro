# Uma imagem para todos os processos do livro; o comando de cada serviço está no compose.yml.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
COPY prumo prumo
COPY scripts scripts
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app
