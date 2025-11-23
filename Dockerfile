FROM python:3.11-slim

WORKDIR /app

# Systemabhängigkeiten nach Bedarf (poppler, etc., falls später nötig)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install "llama-index-embeddings-ollama"

COPY app ./app
COPY data ./data
COPY prompt1.txt ./prompt1.txt
COPY prompt2.txt ./prompt2.txt

ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "-m", "app.app"]
