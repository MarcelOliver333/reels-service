FROM python:3.12-slim

# System deps: ffmpeg, auto-editor, fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir auto-editor

COPY . .

RUN mkdir -p /app/workdata

EXPOSE 3001

CMD ["gunicorn", "--bind", "0.0.0.0:3001", "--timeout", "600", "--workers", "1", "app:app"]
