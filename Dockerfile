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
COPY poses/ poses/

# 作業ディレクトリを作成
RUN mkdir -p /tmp/video_merge /tmp/video_output /tmp/audio_upload /tmp/video_jobs

EXPOSE 10000

CMD ["sh", "-c", "python app.py"]
