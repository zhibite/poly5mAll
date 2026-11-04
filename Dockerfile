FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.toml ./
COPY src ./src

# data/ is a volume in docker-compose; create it for standalone runs.
RUN mkdir -p /app/data

CMD ["python", "-m", "src.main"]
