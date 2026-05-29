FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    HF_HOME=/opt/hf-cache \
    XDG_CACHE_HOME=/opt/xdg-cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libgomp1 \
    libxml2-dev \
    libxslt1-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r /tmp/requirements.txt && \
    python -m nltk.downloader punkt punkt_tab stopwords

COPY . /app

RUN mkdir -p /app/logs /app/Data /opt/hf-cache /opt/xdg-cache

EXPOSE 8501

CMD ["streamlit", "run", "Frontend/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.fileWatcherType=none"]
