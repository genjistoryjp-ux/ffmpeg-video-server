FROM python:3.11-slim

# FFmpegと日本語フォントをインストール
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 作業ディレクトリを作成
RUN mkdir -p /tmp/video_merge /tmp/video_output /tmp/audio_upload /tmp/video_jobs

# Render.comの$PORTを使用、gthreadワーカー、タイムアウト無効
EXPOSE 10000

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --worker-class gthread --threads 4 --timeout 0 --workers 1 --keep-alive 65 --graceful-timeout 0"]
