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

# Documentation only: the app actually listens on config.toml [WEB] PORT
# (8033) or the WEB_PORT environment variable.  Publish it with
# `docker run -p 8033:8033 ...` or the compose `ports` mapping.
EXPOSE 8033

CMD ["python", "-m", "src.main"]
