FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite+aiosqlite:////var/data/shop.db \
    API_PROVIDERS_FILE=/var/data/providers.json \
    RENDER_DATA_DIR=/var/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /var/data

VOLUME ["/var/data"]
CMD ["python", "render_entrypoint.py"]
