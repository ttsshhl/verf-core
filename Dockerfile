FROM python:3.12-slim

RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY vendor ./vendor
RUN pip install --no-cache-dir --no-index --find-links ./vendor -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
