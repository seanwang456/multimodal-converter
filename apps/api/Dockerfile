FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 系统级依赖：LibreOffice（Office 转换）+ ffmpeg（音视频）+ CJK 字体（中文渲染）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice ffmpeg fonts-noto-cjk fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install ".[convert]"

ENV STORAGE_ROOT=/app/storage \
    SQLITE_PATH=/app/storage/app.db \
    REDIS_URL=redis://redis:6379/0

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
