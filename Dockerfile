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
RUN mkdir -p /tmp/video_merge /tmp/video_output

EXPOSE 5555

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5555", "--timeout", "600", "--workers", "1"]
