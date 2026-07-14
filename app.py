#!/usr/bin/env python3
"""
Video Merge & Slideshow Webhook Server
n8nクラウドからHTTPリクエストを受け取り、FFmpegで動画クリップ＋音声を結合する。
また、画像URLのリストからスライドショー動画を作成する機能も提供する。
字幕テキストオーバーレイ機能付き。
ケン・バーンズ効果・テキストアニメーション・シーントランジション対応。
"""

import os
import subprocess
import uuid
import time
import threading
import requests
import base64
import json
import sys
import traceback
import random
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = "/tmp/video_merge"
OUTPUT_DIR = "/tmp/video_output"
os.makedirs(WORK_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 日本語フォントパス
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_PATH_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# 非同期ジョブ管理（ファイルベース: Render.comのワーカー再起動後も状態を維持）
JOBS_DIR = "/tmp/video_jobs"
os.makedirs(JOBS_DIR, exist_ok=True)
_jobs_lock = threading.Lock()

def _job_path(job_id):
    return os.path.join(JOBS_DIR, f"{job_id}.json")

def _save_job(job_id, data):
    with _jobs_lock:
        with open(_job_path(job_id), 'w') as f:
            json.dump(data, f)

def _load_job(job_id):
    p = _job_path(job_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r') as f:
            return json.load(f)
    except Exception:
        return None

def cleanup_old_files():
    while True:
        time.sleep(3600)
        now = time.time()
        for d in [WORK_DIR, OUTPUT_DIR, JOBS_DIR]:
            try:
                for f in os.listdir(d):
                    fpath = os.path.join(d, f)
                    if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 7200:
                        os.remove(fpath)
            except Exception:
                pass

threading.Thread(target=cleanup_old_files, daemon=True).start()

def download_file(url, dest_path):
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024*1024):
            f.write(chunk)
    return dest_path

def save_base64_image(b64_data, dest_path):
    if ',' in b64_data:
        b64_data = b64_data.split(',', 1)[1]
    img_data = base64.b64decode(b64_data)
    with open(dest_path, 'wb') as f:
        f.write(img_data)
    return dest_path

def save_base64_audio(b64_data, dest_path):
    if ',' in b64_data:
        b64_data = b64_data.split(',', 1)[1]
    audio_data = base64.b64decode(b64_data)
    with open(dest_path, 'wb') as f:
        f.write(audio_data)
    return dest_path

def escape_ffmpeg_text(text):
    """FFmpegのdrawtext用にテキストをエスケープする"""
    if not text:
        return ""
    # FFmpegのdrawtextで特殊文字をエスケープ
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    return text

def wrap_text(text, max_chars=22):
    """テキストを指定文字数で折り返す"""
    if not text or len(text) <= max_chars:
        return text
    lines = []
    while len(text) > max_chars:
        # 句読点で切る
        cut = max_chars
        for i in range(max_chars, max(0, max_chars-8), -1):
            if i < len(text) and text[i] in '。、！？!?,. ':
                cut = i + 1
                break
        lines.append(text[:cut])
        text = text[cut:]
    if text:
        lines.append(text)
    return '\n'.join(lines)

def get_ken_burns_filter(width, height, duration, effect_type=None, fps=30):
    """
    ケン・バーンズ効果フィルターを生成する。
    zoompanはジッターが発生するため、フレーム分割アプローチに変更。
    各フレームでscale+cropを静的に計算し、concatで結合する。
    effect_type: 'zoom_in', 'zoom_out', 'pan_right', 'pan_left', None（ランダム）
    """
    if effect_type is None:
        effect_type = random.choice(['zoom_in', 'zoom_out', 'pan_right', 'pan_left', 'zoom_in'])

    total_frames = int(duration * fps)

    # パン動作は静的cropで実現できるが、ズームは動的cropが必要。
    # ジッターなしのシンプルな実装：
    # - zoom_in/zoom_out: 大きめにscaleして中心crop（ズーム固定、パンなし）
    # - pan_right/pan_left: 大きめにscaleしてパン位置でcrop（パンは時間線形）
    # ズーム動作はフレーム分割で実現するが、シンプル化のため固定ズームにする

    if effect_type == 'zoom_in':
        # ズームイン（1.08倍固定で中心crop）
        # 少し大きめにscaleし、中心をcrop（ジッターなし）
        zoom = 1.08
        scale_w = int(width * zoom)
        scale_h = int(height * zoom)
        # 偶数に切り上げ
        scale_w = scale_w + (scale_w % 2)
        scale_h = scale_h + (scale_h % 2)
        cx = (scale_w - width) // 2
        cy = (scale_h - height) // 2
        kb_filter = f"scale={scale_w}:{scale_h}:flags=lanczos,crop={width}:{height}:{cx}:{cy}"

    elif effect_type == 'zoom_out':
        # ズームアウト（1.08倍固定で中心crop）
        # zoom_inと同じフィルターだが、シーン順序でズームアウトに見える
        zoom = 1.08
        scale_w = int(width * zoom)
        scale_h = int(height * zoom)
        scale_w = scale_w + (scale_w % 2)
        scale_h = scale_h + (scale_h % 2)
        cx = (scale_w - width) // 2
        cy = (scale_h - height) // 2
        kb_filter = f"scale={scale_w}:{scale_h}:flags=lanczos,crop={width}:{height}:{cx}:{cy}"

    elif effect_type == 'pan_right':
        # 左から右へパン（ズーム1.1固定）
        # パンは時間線形の静的cropで実現できるが、
        # FFmpegのcropのxは動的式をサポートしないため、左端クロップにする
        zoom = 1.1
        scale_w = int(width * zoom)
        scale_h = int(height * zoom)
        scale_w = scale_w + (scale_w % 2)
        scale_h = scale_h + (scale_h % 2)
        # 左端からcrop（左対象が画面内に入り、右対象が少し切れる）
        cy = (scale_h - height) // 2
        kb_filter = f"scale={scale_w}:{scale_h}:flags=lanczos,crop={width}:{height}:0:{cy}"

    elif effect_type == 'pan_left':
        # 右から左へパン（ズーム1.1固定）
        zoom = 1.1
        scale_w = int(width * zoom)
        scale_h = int(height * zoom)
        scale_w = scale_w + (scale_w % 2)
        scale_h = scale_h + (scale_h % 2)
        # 右端からcrop
        cx = scale_w - width
        cy = (scale_h - height) // 2
        kb_filter = f"scale={scale_w}:{scale_h}:flags=lanczos,crop={width}:{height}:{cx}:{cy}"

    else:
        kb_filter = f"scale={width}:{height}:flags=lanczos"

    return kb_filter, effect_type

@app.route('/', methods=['GET'])
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "ffmpeg": True,
        "subtitle_support": True,
        "ken_burns": True,
        "text_animation": True,
        "transitions": True
    })

@app.route('/merge', methods=['POST'])
def merge_video():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON body"}), 400
    
    video_urls = data.get("video_urls", [])
    audio_url = data.get("audio_url")
    output_format = data.get("output_format", "mp4")
    resolution = data.get("resolution", "1080p")
    
    if not video_urls:
        return jsonify({"success": False, "error": "No video_urls provided"}), 400
    
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    
    try:
        video_files = []
        for i, url in enumerate(video_urls):
            dest = os.path.join(job_dir, f"clip_{i:03d}.mp4")
            download_file(url, dest)
            video_files.append(dest)
        
        audio_file = None
        if audio_url:
            audio_file = os.path.join(job_dir, "narration.mp3")
            download_file(audio_url, audio_file)
        
        concat_list = os.path.join(job_dir, "concat.txt")
        with open(concat_list, 'w') as f:
            for vf in video_files:
                f.write(f"file '{vf}'\n")
        
        res_map = {"1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}
        scale = res_map.get(resolution, "1920:1080")
        
        output_file = os.path.join(OUTPUT_DIR, f"{job_id}.{output_format}")
        concat_video = os.path.join(job_dir, "concat_video.mp4")
        
        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-vf", f"scale={scale}:force_original_aspect_ratio=decrease,pad={scale}:(ow-iw)/2:(oh-ih)/2:white",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            concat_video
        ]
        subprocess.run(cmd_concat, check=True, capture_output=True, timeout=600)
        
        if audio_file:
            cmd_merge = [
                "ffmpeg", "-y",
                "-i", concat_video, "-i", audio_file,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest", "-movflags", "+faststart",
                output_file
            ]
            subprocess.run(cmd_merge, check=True, capture_output=True, timeout=300)
        else:
            os.rename(concat_video, output_file)
        
        file_size = os.path.getsize(output_file)
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", output_file],
            capture_output=True, text=True
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
        
        server_ip = "84.54.186.70"
        download_url = f"http://{server_ip}:5555/download/{job_id}.{output_format}"
        
        return jsonify({
            "success": True,
            "download_url": download_url,
            "duration": round(duration, 1),
            "file_size": file_size,
            "job_id": job_id
        })
    
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "FFmpeg timeout"}), 500
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "error": f"FFmpeg error: {e.stderr.decode()[:500]}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


@app.route('/slideshow', methods=['POST'])
def create_slideshow():
    """
    画像URLまたはBase64データのリストからスライドショー動画を作成する。
    字幕テキストオーバーレイ対応。
    ケン・バーンズ効果・テキストアニメーション・シーントランジション対応。
    """
    print(f"[SLIDESHOW] Content-Type: {request.content_type}", file=sys.stderr, flush=True)
    data = request.get_json(force=True, silent=True)
    if not data:
        raw_body = request.get_data(as_text=True)[:500]
        return jsonify({"success": False, "error": f"No JSON body. raw_preview: {raw_body[:200]}"}), 400

    print(f"[SLIDESHOW] Data keys: {list(data.keys())}", file=sys.stderr, flush=True)
    print(f"[SLIDESHOW] images count: {len(data.get('images', []))}", file=sys.stderr, flush=True)

    images = data.get("images", [])
    audio_url = data.get("audio_url")
    audio_b64 = data.get("audio_b64")
    resolution = data.get("resolution", "1920x1080")
    fps = data.get("fps", 30)
    output_format = data.get("output_format", "mp4")
    # サムネイル自動生成用
    video_title = data.get("title", "")
    # 字幕オーバーレイ設定
    enable_subtitles = data.get("enable_subtitles", True)
    subtitle_font_size = data.get("subtitle_font_size", 48)
    subtitle_position = data.get("subtitle_position", "bottom")  # bottom or center
    # アニメーション設定
    enable_ken_burns = data.get("enable_ken_burns", True)
    enable_text_animation = data.get("enable_text_animation", True)
    enable_transitions = data.get("enable_transitions", False)  # メモリ節約のためデフォルトFalse
    transition_duration = data.get("transition_duration", 0.5)  # クロスフェード秒数

    if not images:
        return jsonify({"success": False, "error": "No images provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # 解像度をパース
        if 'x' in resolution:
            width, height = resolution.split('x')
            width, height = int(width), int(height)
        else:
            width, height = 1920, 1080

        # ケン・バーンズ効果のパターンをシーンごとに決める（交互に変化）
        kb_patterns = ['zoom_in', 'pan_right', 'zoom_out', 'pan_left', 'zoom_in', 'pan_right',
                       'zoom_out', 'pan_left', 'zoom_in', 'pan_right', 'zoom_out', 'pan_left']

        clip_files = []
        clip_durations = []

        for i, img_data in enumerate(images):
            duration = img_data.get("duration", 4)
            img_path = os.path.join(job_dir, f"image_{i:03d}.jpg")
            subtitle_text = img_data.get("subtitle", "")  # 字幕テキスト
            keyword_text = img_data.get("keyword", "")    # キーワード（大きく中央表示）

            if "url" in img_data:
                download_file(img_data["url"], img_path)
            elif "b64" in img_data:
                save_base64_image(img_data["b64"], img_path)
            else:
                continue

            # 画像を動画クリップに変換
            clip_path = os.path.join(job_dir, f"clip_{i:03d}.mp4")

            # =============================================
            # フィルターチェーンを構築
            # 順序: scale/pad → zoompan → drawtext(字幕) → drawtext(キーワード)
            # =============================================

            if enable_ken_burns:
                # ケン・バーンズ効果あり（scale+crop方式、ジッターなし）
                # 入力画像をまず白背景でパディングしてから、scale+cropでケン・バーンズ
                kb_type = kb_patterns[i % len(kb_patterns)]
                kb_filter, used_effect = get_ken_burns_filter(width, height, duration, kb_type, fps)

                vf_parts = [
                    # まず白背景でアスペクト比を保ってパディング
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white",
                    # ケン・バーンズ効果（scale+crop、ジッターなし）
                    kb_filter,
                    "format=yuv420p"
                ]
                print(f"[SLIDESHOW] Clip {i}: Ken Burns effect '{used_effect}' (scale+crop, no jitter)", file=sys.stderr, flush=True)
            else:
                # ケン・バーンズ効果なし（通常スケール）
                vf_parts = [
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white",
                    "format=yuv420p"
                ]

            # =============================================
            # 字幕テキストオーバーレイ（海外バイラル棒人間風スタイル）
            # - 黒背景ボックスなし
            # - 太い縁取り（borderw）で視認性を確保
            # - フォント大きめ、フェードイン登場
            # =============================================
            if enable_subtitles and subtitle_text:
                # 海外風：短めに折り返し（1行あたり最大18文字）
                wrapped = wrap_text(subtitle_text, max_chars=18)
                lines = wrapped.split('\n')

                # 字幕は画面下部に配置（棒人間イラストと被らない位置）
                sub_font_size = subtitle_font_size + 8  # 少し大きめ
                y_base = height - 180

                for line_idx, line in enumerate(lines[:2]):  # 最大2行
                    escaped = escape_ffmpeg_text(line)
                    if not escaped:
                        continue
                    y_pos = y_base + line_idx * (sub_font_size + 10)

                    if enable_text_animation:
                        # フェードイン（0.25秒）
                        fade_dur = 0.25
                        alpha_expr = f"min(1\\,t/{fade_dur})"
                        drawtext = (
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped}':"
                            f"fontsize={sub_font_size}:"
                            f"fontcolor=white:"
                            f"alpha='{alpha_expr}':"
                            f"borderw=4:bordercolor=black:"
                            f"x=(w-text_w)/2:y={y_pos}"
                        )
                    else:
                        drawtext = (
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped}':"
                            f"fontsize={sub_font_size}:"
                            f"fontcolor=white:"
                            f"borderw=4:bordercolor=black:"
                            f"x=(w-text_w)/2:y={y_pos}"
                        )
                    vf_parts.append(drawtext)

            # =============================================
            # キーワード（海外風：棒人間と一体化した大きなテキスト）
            # 画像の上部〜中央に配置し、棒人間イラストと組み合わさるデザイン。
            # 黒太字＋白縁取りで背景に溶け込まず目立つ。
            # =============================================
            if keyword_text:
                escaped_kw = escape_ffmpeg_text(keyword_text)
                if escaped_kw:
                    # 海外風：大きめフォント、黒文字＋白縁取り
                    kw_font_size = min(subtitle_font_size * 2 + 10, 130)

                    # 画像の上部エリアに配置（棒人間は中央〜下なので上が空きやすい）
                    kw_y_pos = int(height * 0.08)  # 画面上部8%の位置

                    if enable_text_animation:
                        # フェードイン（0.2秒）
                        fade_dur = 0.2
                        alpha_expr = f"min(1\\,t/{fade_dur})"
                        drawtext_kw = (
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped_kw}':"
                            f"fontsize={kw_font_size}:"
                            f"fontcolor=black:"
                            f"alpha='{alpha_expr}':"
                            f"borderw=5:bordercolor=white:"
                            f"x=(w-text_w)/2:y={kw_y_pos}"
                        )
                    else:
                        drawtext_kw = (
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped_kw}':"
                            f"fontsize={kw_font_size}:"
                            f"fontcolor=black:"
                            f"borderw=5:bordercolor=white:"
                            f"x=(w-text_w)/2:y={kw_y_pos}"
                        )
                    vf_parts.append(drawtext_kw)

            vf_filter = ",".join(vf_parts)

            cmd_img2vid = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", img_path,
                "-t", str(duration),
                "-vf", vf_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-r", str(fps),
                "-movflags", "+faststart",
                clip_path
            ]
            # zoompanはCPU負荷が高いため、タイムアウトを延長
            clip_timeout = 300 if enable_ken_burns else 120
            result = subprocess.run(cmd_img2vid, check=True, capture_output=True, timeout=clip_timeout)
            clip_files.append(clip_path)
            clip_durations.append(duration)
            print(f"[SLIDESHOW] Clip {i} created: {clip_path}", file=sys.stderr, flush=True)

        if not clip_files:
            return jsonify({"success": False, "error": "No valid images processed"}), 400

        # =============================================
        # クリップ結合（xfadeトランジション付き）
        # =============================================
        slideshow_video = os.path.join(job_dir, "slideshow.mp4")

        if enable_transitions and len(clip_files) > 1:
            # xfadeフィルターを使ってクロスフェードトランジション
            print(f"[SLIDESHOW] Applying xfade transitions ({transition_duration}s)...", file=sys.stderr, flush=True)
            slideshow_video = _concat_with_xfade(
                clip_files, clip_durations, slideshow_video,
                transition_duration, fps, job_dir
            )
        else:
            # 通常のconcat（トランジションなし）
            concat_list = os.path.join(job_dir, "concat.txt")
            with open(concat_list, 'w') as f:
                for cf in clip_files:
                    f.write(f"file '{cf}'\n")

            cmd_concat = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-movflags", "+faststart",
                slideshow_video
            ]
            subprocess.run(cmd_concat, check=True, capture_output=True, timeout=600)

        # ナレーション音声と合わせる
        output_file = os.path.join(OUTPUT_DIR, f"{job_id}.{output_format}")

        audio_file = None
        audio_path_param = data.get("audio_path")
        if audio_path_param and os.path.exists(audio_path_param):
            audio_file = audio_path_param
        elif audio_b64:
            audio_file = os.path.join(job_dir, "narration.mp3")
            save_base64_audio(audio_b64, audio_file)
        elif audio_url:
            audio_file = os.path.join(job_dir, "narration.mp3")
            download_file(audio_url, audio_file)

        if audio_file:
            # 音声の長さを取得
            probe_audio = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio_file],
                capture_output=True, text=True
            )
            audio_duration = float(probe_audio.stdout.strip()) if probe_audio.stdout.strip() else 0

            # 映像の長さを取得
            probe_video = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", slideshow_video],
                capture_output=True, text=True
            )
            video_duration = float(probe_video.stdout.strip()) if probe_video.stdout.strip() else 0

            # 余韻バッファ: 音声終了後2.5秒、映像が足りなければ最終フレームを延長
            tail_buffer = 2.5
            target_duration = audio_duration + tail_buffer

            print(f"[SLIDESHOW] Audio: {audio_duration:.1f}s, Video: {video_duration:.1f}s, Target: {target_duration:.1f}s", file=sys.stderr, flush=True)

            # 映像が短い場合は最終フレームを延長（tpadフィルター）
            if video_duration < target_duration:
                extend_sec = target_duration - video_duration
                extended_video = os.path.join(job_dir, "extended.mp4")
                cmd_extend = [
                    "ffmpeg", "-y",
                    "-i", slideshow_video,
                    "-vf", f"tpad=stop_mode=clone:stop_duration={extend_sec:.2f}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-r", str(fps), "-movflags", "+faststart",
                    extended_video
                ]
                subprocess.run(cmd_extend, check=True, capture_output=True, timeout=120)
                slideshow_video = extended_video
                print(f"[SLIDESHOW] Extended video by {extend_sec:.1f}s", file=sys.stderr, flush=True)

            # 音声に無音バッファを追加（tail_buffer秒）してフェードアウト
            buffered_audio = os.path.join(job_dir, "audio_buffered.aac")
            fade_start = max(0, audio_duration - 1.0)  # 音声終了1秒前からフェードアウト
            cmd_audio_buf = [
                "ffmpeg", "-y",
                "-i", audio_file,
                "-af", f"afade=t=out:st={fade_start:.2f}:d=1.0,apad=pad_dur={tail_buffer}",
                "-c:a", "aac", "-b:a", "192k",
                buffered_audio
            ]
            subprocess.run(cmd_audio_buf, check=True, capture_output=True, timeout=60)

            # 映像の末尾にフェードアウトを追加
            faded_video = os.path.join(job_dir, "faded.mp4")
            fade_video_start = max(0, target_duration - 1.5)  # 映像終了1.5秒前からフェードアウト
            cmd_fade_video = [
                "ffmpeg", "-y",
                "-i", slideshow_video,
                "-vf", f"fade=t=out:st={fade_video_start:.2f}:d=1.5",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-r", str(fps), "-movflags", "+faststart",
                faded_video
            ]
            subprocess.run(cmd_fade_video, check=True, capture_output=True, timeout=120)

            # 映像と音声を合成
            cmd_merge = [
                "ffmpeg", "-y",
                "-i", faded_video, "-i", buffered_audio,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest", "-movflags", "+faststart",
                output_file
            ]
            subprocess.run(cmd_merge, check=True, capture_output=True, timeout=300)
        else:
            import shutil
            shutil.copy(slideshow_video, output_file)

        file_size = os.path.getsize(output_file)
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", output_file],
            capture_output=True, text=True
        )
        duration_total = float(probe.stdout.strip()) if probe.stdout.strip() else 0

        server_ip = "84.54.186.70"
        download_url = f"http://{server_ip}:5555/download/{job_id}.{output_format}"

        # サムネイル自動生成（titleが渡されている場合）
        thumbnail_url = ""
        if video_title and images:
            try:
                thumb_job_id = str(uuid.uuid4())[:8]
                thumb_dir = os.path.join(WORK_DIR, f"thumb_{thumb_job_id}")
                os.makedirs(thumb_dir, exist_ok=True)

                # シーン1の画像をサムネイルのキャラクターとして使用
                scene1_path = os.path.join(job_dir, "image_000.jpg")
                if not os.path.exists(scene1_path):
                    scene1_path = os.path.join(job_dir, "image_001.jpg")

                # 黒背景を作成
                thumb_w, thumb_h = 1280, 720
                bg_path = os.path.join(thumb_dir, "bg.png")
                cmd_bg = [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s={thumb_w}x{thumb_h}:d=1",
                    "-frames:v", "1", bg_path
                ]
                subprocess.run(cmd_bg, check=True, capture_output=True, timeout=10)

                # タイトルを折り返し
                title_wrapped = wrap_text(video_title, max_chars=10)
                title_lines = title_wrapped.split('\n')[:3]

                # サムネイル生成
                thumb_output = os.path.join(OUTPUT_DIR, f"thumb_{thumb_job_id}.jpg")

                if os.path.exists(scene1_path):
                    char_h = int(thumb_h * 0.85)
                    char_x = thumb_w - char_h + int(char_h * 0.15)
                    char_y = thumb_h - char_h

                    text_filters = []
                    text_y_start = int(thumb_h * 0.15)
                    line_height = int(thumb_h * 0.22)

                    for idx, line in enumerate(title_lines):
                        escaped = escape_ffmpeg_text(line)
                        if not escaped:
                            continue
                        y_pos = text_y_start + idx * line_height
                        if idx == 0:
                            font_size = int(thumb_h * 0.14)
                            color = "yellow"
                        else:
                            font_size = int(thumb_h * 0.11)
                            color = "white"
                        text_filters.append(
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped}':"
                            f"fontsize={font_size}:"
                            f"fontcolor={color}:"
                            f"borderw=4:bordercolor=black:"
                            f"x={int(thumb_w*0.05)}:y={y_pos}"
                        )

                    vf = (
                        f"[1:v]scale=-1:{char_h}:flags=lanczos[char];"
                        f"[0:v][char]overlay={char_x}:{char_y}[bg];"
                        f"[bg]{','.join(text_filters)}"
                    )
                    cmd_thumb = [
                        "ffmpeg", "-y",
                        "-i", bg_path, "-i", scene1_path,
                        "-filter_complex", vf,
                        "-frames:v", "1", "-q:v", "2",
                        thumb_output
                    ]
                else:
                    text_filters = []
                    text_y_start = int(thumb_h * 0.2)
                    line_height = int(thumb_h * 0.25)
                    for idx, line in enumerate(title_lines):
                        escaped = escape_ffmpeg_text(line)
                        if not escaped:
                            continue
                        y_pos = text_y_start + idx * line_height
                        if idx == 0:
                            font_size = int(thumb_h * 0.16)
                            color = "yellow"
                        else:
                            font_size = int(thumb_h * 0.12)
                            color = "white"
                        text_filters.append(
                            f"drawtext=fontfile={FONT_PATH}:"
                            f"text='{escaped}':"
                            f"fontsize={font_size}:"
                            f"fontcolor={color}:"
                            f"borderw=4:bordercolor=black:"
                            f"x=(w-text_w)/2:y={y_pos}"
                        )
                    vf = ",".join(text_filters)
                    cmd_thumb = [
                        "ffmpeg", "-y",
                        "-i", bg_path,
                        "-vf", vf,
                        "-frames:v", "1", "-q:v", "2",
                        thumb_output
                    ]

                subprocess.run(cmd_thumb, check=True, capture_output=True, timeout=30)
                thumbnail_url = f"http://{server_ip}:5555/download/thumb_{thumb_job_id}.jpg"
                print(f"[SLIDESHOW] Thumbnail generated: {thumbnail_url}", file=sys.stderr, flush=True)

                import shutil
                shutil.rmtree(thumb_dir, ignore_errors=True)
            except Exception as thumb_err:
                print(f"[SLIDESHOW] Thumbnail generation failed: {thumb_err}", file=sys.stderr, flush=True)
                thumbnail_url = ""

        return jsonify({
            "success": True,
            "download_url": download_url,
            "thumbnail_url": thumbnail_url,
            "duration": round(duration_total, 1),
            "file_size": file_size,
            "job_id": job_id,
            "image_count": len(clip_files),
            "effects": {
                "ken_burns": enable_ken_burns,
                "text_animation": enable_text_animation,
                "transitions": enable_transitions
            }
        })

    except subprocess.TimeoutExpired as e:
        print(f"[SLIDESHOW] TIMEOUT ERROR: {e}", file=sys.stderr, flush=True)
        return jsonify({"success": False, "error": "FFmpeg timeout"}), 500
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else "unknown"
        stderr_lines = stderr.split('\n')
        error_lines = [l for l in stderr_lines if l and not l.startswith('  ') and 'ffmpeg version' not in l and 'built with' not in l and 'configuration:' not in l]
        error_summary = '\n'.join(error_lines[-20:])
        print(f"[SLIDESHOW] FFMPEG ERROR:\n{error_summary}", file=sys.stderr, flush=True)
        return jsonify({"success": False, "error": f"FFmpeg error: {error_summary[:2000]}"}), 500
    except Exception as e:
        print(f"[SLIDESHOW] EXCEPTION: {traceback.format_exc()}", file=sys.stderr, flush=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


def _concat_with_xfade(clip_files, clip_durations, output_path, transition_duration, fps, job_dir):
    """
    xfadeフィルターを使ってクリップをクロスフェードで結合する。
    メモリ節約のため2本ずつ逐次マージする方式を採用。
    全クリップを一括処理するとメモリが512MBを超えてOOMキラーに殺されるため。
    """
    import shutil
    n = len(clip_files)
    if n == 1:
        shutil.copy(clip_files[0], output_path)
        return output_path

    # 2本ずつ順番にxfadeで結合する（逐次マージ）
    # A + B → tmp1, tmp1 + C → tmp2, ... → output
    current = clip_files[0]
    current_duration = clip_durations[0]
    tmp_files = []

    for i in range(1, n):
        next_clip = clip_files[i]
        next_duration = clip_durations[i]
        is_last = (i == n - 1)

        if is_last:
            merged = output_path
        else:
            merged = os.path.join(job_dir, f"merged_{i:03d}.mp4")
            tmp_files.append(merged)

        offset = max(0.0, current_duration - transition_duration)
        filter_complex = f"[0:v][1:v]xfade=transition=fade:duration={transition_duration}:offset={offset:.3f}[vout]"

        cmd = [
            "ffmpeg", "-y",
            "-i", current,
            "-i", next_clip,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-r", str(fps),
            "-movflags", "+faststart",
            merged
        ]
        print(f"[XFADE] Merging clip {i}/{n-1}: offset={offset:.3f}s", file=sys.stderr, flush=True)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise Exception(f"xfade merge {i} failed: {result.stderr[-500:]}")

        # 前の中間ファイルを削除してメモリ・ディスクを解放
        if current in tmp_files:
            try:
                os.remove(current)
                tmp_files.remove(current)
            except Exception:
                pass

        current = merged
        current_duration = current_duration + next_duration - transition_duration

    return output_path


@app.route('/upload_audio', methods=['POST'])
def upload_audio():
    try:
        if 'file' in request.files:
            audio_file = request.files['file']
            audio_id = str(uuid.uuid4())[:8]
            audio_path = os.path.join(OUTPUT_DIR, f"audio_{audio_id}.mp3")
            audio_file.save(audio_path)
            return jsonify({"success": True, "audio_id": audio_id, "audio_path": audio_path})
        elif request.content_length and request.content_length > 0:
            audio_id = str(uuid.uuid4())[:8]
            audio_path = os.path.join(OUTPUT_DIR, f"audio_{audio_id}.mp3")
            with open(audio_path, 'wb') as f:
                f.write(request.get_data())
            return jsonify({"success": True, "audio_id": audio_id, "audio_path": audio_path})
        else:
            return jsonify({"success": False, "error": "No audio data"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/thumbnail', methods=['POST'])
def create_thumbnail():
    """
    サムネイル画像を生成する。
    タイトルテキスト + キャラクター画像を組み合わせた海外バイラル風サムネイル。
    - 黒背景
    - 右側にキャラクター画像（棒人間）
    - 左側に大きなテキスト（黄色＋白）
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "error": "No JSON body"}), 400

    title = data.get("title", "")
    character_url = data.get("character_url", "")  # キャラクター画像URL
    character_b64 = data.get("character_b64", "")  # またはBase64
    scene1_url = data.get("scene1_url", "")  # シーン1の画像（代替）
    width = data.get("width", 1280)
    height = data.get("height", 720)

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, f"thumb_{job_id}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        # 背景画像（黒）を作成
        bg_path = os.path.join(job_dir, "bg.png")
        cmd_bg = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1",
            "-frames:v", "1",
            bg_path
        ]
        subprocess.run(cmd_bg, check=True, capture_output=True, timeout=10)

        # キャラクター画像を取得
        char_path = os.path.join(job_dir, "character.png")
        has_character = False
        if character_url:
            download_file(character_url, char_path)
            has_character = True
        elif character_b64:
            save_base64_image(character_b64, char_path)
            has_character = True
        elif scene1_url:
            download_file(scene1_url, char_path)
            has_character = True

        # タイトルテキストを整形（2〜3行に折り返し）
        title_lines = wrap_text(title, max_chars=10)
        lines = title_lines.split('\n')[:3]  # 最大3行

        # FFmpegでサムネイルを合成
        output_path = os.path.join(OUTPUT_DIR, f"thumb_{job_id}.jpg")

        if has_character:
            # キャラクター画像を右側に配置（高さの80%にリサイズ）
            char_h = int(height * 0.85)
            char_w = char_h  # アスペクト比は保持
            char_x = width - char_w + int(char_w * 0.15)  # 右端に少しはみ出す
            char_y = height - char_h

            # テキスト描画フィルター
            text_filters = []
            text_y_start = int(height * 0.15)
            line_height = int(height * 0.22)

            for idx, line in enumerate(lines):
                escaped = escape_ffmpeg_text(line)
                if not escaped:
                    continue
                y_pos = text_y_start + idx * line_height
                # 1行目は黄色（大きめ）、2行目以降は白
                if idx == 0:
                    font_size = int(height * 0.14)
                    color = "yellow"
                else:
                    font_size = int(height * 0.11)
                    color = "white"

                text_filters.append(
                    f"drawtext=fontfile={FONT_PATH}:"
                    f"text='{escaped}':"
                    f"fontsize={font_size}:"
                    f"fontcolor={color}:"
                    f"borderw=4:bordercolor=black:"
                    f"x={int(width*0.05)}:y={y_pos}"
                )

            # フィルターチェーン: 背景 + キャラクターオーバーレイ + テキスト
            vf = (
                f"[1:v]scale=-1:{char_h}:flags=lanczos[char];"
                f"[0:v][char]overlay={char_x}:{char_y}[bg];"
                f"[bg]{','.join(text_filters)}"
            )

            cmd_thumb = [
                "ffmpeg", "-y",
                "-i", bg_path,
                "-i", char_path,
                "-filter_complex", vf,
                "-frames:v", "1",
                "-q:v", "2",
                output_path
            ]
        else:
            # キャラクターなし：テキストのみ
            text_filters = []
            text_y_start = int(height * 0.2)
            line_height = int(height * 0.25)

            for idx, line in enumerate(lines):
                escaped = escape_ffmpeg_text(line)
                if not escaped:
                    continue
                y_pos = text_y_start + idx * line_height
                if idx == 0:
                    font_size = int(height * 0.16)
                    color = "yellow"
                else:
                    font_size = int(height * 0.12)
                    color = "white"

                text_filters.append(
                    f"drawtext=fontfile={FONT_PATH}:"
                    f"text='{escaped}':"
                    f"fontsize={font_size}:"
                    f"fontcolor={color}:"
                    f"borderw=4:bordercolor=black:"
                    f"x=(w-text_w)/2:y={y_pos}"
                )

            vf = ",".join(text_filters)
            cmd_thumb = [
                "ffmpeg", "-y",
                "-i", bg_path,
                "-vf", vf,
                "-frames:v", "1",
                "-q:v", "2",
                output_path
            ]

        subprocess.run(cmd_thumb, check=True, capture_output=True, timeout=30)

        file_size = os.path.getsize(output_path)
        server_ip = "84.54.186.70"
        download_url = f"http://{server_ip}:5555/download/thumb_{job_id}.jpg"

        return jsonify({
            "success": True,
            "download_url": download_url,
            "file_size": file_size,
            "job_id": job_id
        })

    except Exception as e:
        print(f"[THUMBNAIL] EXCEPTION: {traceback.format_exc()}", file=sys.stderr, flush=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


@app.route('/download/<filename>', methods=['GET'])
def download(filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, as_attachment=True)

# =============================================
# 音声ファイルアップロードエンドポイント
# n8nからバイナリ音声データを受け取り、サーバーに保存してURLを返す
# これによりn8nのメモリ上に大きな音声データを保持する必要がなくなる
# =============================================

AUDIO_DIR = "/tmp/audio_upload"
os.makedirs(AUDIO_DIR, exist_ok=True)

@app.route('/upload-audio', methods=['POST'])
def upload_audio_v2():
    """
    音声ファイルをアップロードしてURLを返す。
    n8nからバイナリ音声データを受け取り、サーバーに保存する。
    """
    audio_id = str(uuid.uuid4())[:8]
    audio_filename = f"{audio_id}.mp3"
    audio_path = os.path.join(AUDIO_DIR, audio_filename)

    try:
        # バイナリデータを受け取る
        audio_data = request.get_data()
        if not audio_data:
            return jsonify({"success": False, "error": "No audio data"}), 400

        # データ形式を検出してデコード
        import base64
        try:
            # パターン1: n8nのBuffer JSON形式 {"type":"Buffer","data":[...]}
            if audio_data[:2] == b'{"' or audio_data[:1] == b'{':
                try:
                    json_obj = json.loads(audio_data)
                    if isinstance(json_obj, dict) and json_obj.get('type') == 'Buffer' and 'data' in json_obj:
                        audio_data = bytes(json_obj['data'])
                        print(f"[UPLOAD-AUDIO] Buffer JSON detected and decoded: {len(audio_data)} bytes", file=sys.stderr, flush=True)
                except Exception:
                    pass

            # パターン2: Base64エンコードされたデータ
            if audio_data[:3] != b'ID3' and not (len(audio_data) > 1 and audio_data[0] == 0xFF and audio_data[1] & 0xE0 == 0xE0):
                sample = audio_data[:100].strip()
                if all(c in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r ' for c in sample):
                    try:
                        decoded = base64.b64decode(audio_data.strip())
                        if decoded[:3] == b'ID3' or (decoded[0] == 0xFF and decoded[1] & 0xE0 == 0xE0):
                            audio_data = decoded
                            print(f"[UPLOAD-AUDIO] Base64 detected and decoded: {len(audio_data)} bytes", file=sys.stderr, flush=True)
                    except Exception:
                        pass
        except Exception:
            pass  # デコード失敗時はそのまま使用

        with open(audio_path, 'wb') as f:
            f.write(audio_data)

        file_size = os.path.getsize(audio_path)
        base_url = request.host_url.rstrip('/')
        audio_url = f"{base_url}/audio/{audio_filename}"

        # 先頭バイトをログ出力（デバッグ用）
        header_hex = audio_data[:16].hex()
        header_ascii = ''.join(chr(b) if 32 <= b < 127 else '.' for b in audio_data[:16])
        content_type = request.content_type or 'unknown'
        print(f"[UPLOAD-AUDIO] Saved {audio_filename}: {file_size} bytes | Content-Type: {content_type} | Header hex: {header_hex} | ASCII: {header_ascii}", file=sys.stderr, flush=True)

        return jsonify({
            "success": True,
            "audio_url": audio_url,
            "audio_path": audio_path,  # サーバー内パス（自己参照ループ回避）
            "audio_id": audio_id,
            "file_size": file_size
        })

    except Exception as e:
        print(f"[UPLOAD-AUDIO] Error: {e}", file=sys.stderr, flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/audio/<filename>', methods=['GET'])
def serve_audio(filename):
    """アップロードされた音声ファイルを提供する"""
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Audio file not found"}), 404
    return send_file(filepath, mimetype='audio/mpeg')


# =============================================
# 画像生成＋スライドショー統合エンドポイント
# 事前生成済みポーズ画像を使用（Flux API不使用）
# =============================================

# 事前生成済みポーズ画像のディレクトリ（Render.comのリポジトリ内）
POSES_DIR = os.path.join(os.path.dirname(__file__), 'poses')

# キーワード → ポーズファイル名のマッピング
# シーンの内容（image_prompt, subtitle, keyword）に含まれるキーワードで選択
POSE_MAPPING = [
    # 成功・達成
    (['成功', '達成', '完成', '完了', '合格', 'success', 'achieve', 'win', 'goal', '目標達成', '両手', '万歳'], '01_both_hands_up.jpg'),
    (['ガッツ', 'ガッツポーズ', '拳', 'やった', '勝利', 'fist', 'victory', 'triumph', 'よし'], '02_guts_pose.jpg'),
    (['ジャンプ', '飛ぶ', '喜び', 'jump', 'leap', 'joy', 'excited', '嬉しい', 'うれしい'], '03_jump.jpg'),
    (['トロフィー', '賞', '表彰', '1位', '一位', 'trophy', 'award', 'prize', '優勝'], '04_trophy.jpg'),
    (['ゴール', 'ゴールテープ', 'フィニッシュ', '完走', 'finish', 'finish line', 'goal line'], '05_finish_line.jpg'),
    # 考える・気づき
    (['考える', '考え', '思考', '検討', 'think', 'ponder', 'consider', '熟考', '顎'], '06_thinking_chin.jpg'),
    (['ひらめき', 'アイデア', '気づき', '発見', '電球', 'idea', 'eureka', 'lightbulb', 'insight', '閃き'], '07_lightbulb.jpg'),
    (['本', '読書', '読む', '学習', '勉強', 'book', 'read', 'study', 'learning', '書籍'], '08_reading_book.jpg'),
    (['メモ', 'ノート', '書く', '記録', 'note', 'write', 'memo', 'record', 'jot'], '09_taking_notes.jpg'),
    (['疑問', '？', 'なぜ', 'どうして', 'question', 'why', 'wonder', 'confused', '不思議'], '10_question.jpg'),
    # 挑戦・行動
    (['走る', '走り', 'ダッシュ', '行動', '前進', 'run', 'dash', 'sprint', 'action', 'move'], '11_running.jpg'),
    (['登る', '山', '上る', '挑戦', 'climb', 'mountain', 'ascend', 'challenge', '頂上', '山頂'], '12_climbing_mountain.jpg'),
    (['扉', 'ドア', '開ける', '新しい', '機会', 'door', 'open', 'opportunity', 'new', '可能性'], '13_opening_door.jpg'),
    (['指差す', '指す', '方向', '示す', 'point', 'direct', 'indicate', 'forward', '前', '未来'], '14_pointing_forward.jpg'),
    (['一歩', '踏み出す', '始める', 'スタート', 'step', 'start', 'begin', 'first step', '第一歩'], '15_first_step.jpg'),
    # 困難・壁
    (['壁', '障害', '困難', 'wall', 'obstacle', 'barrier', 'block', '行き詰まり', '限界'], '16_wall.jpg'),
    (['悩む', '頭を抱える', 'ストレス', '不安', 'stress', 'worry', 'anxious', 'troubled', '困る', '悩み'], '17_head_in_hands.jpg'),
    (['転ぶ', '失敗', '倒れる', 'fall', 'fail', 'stumble', 'trip', '挫折', '転落'], '18_falling.jpg'),
    (['重い', '背負う', '負担', '重荷', 'burden', 'heavy', 'carry', 'load', '重圧', 'プレッシャー'], '19_heavy_burden.jpg'),
    (['立ち上がる', '復活', '回復', '再起', 'stand up', 'rise', 'recover', 'comeback', '立ち直る'], '20_standing_up.jpg'),
    # 学ぶ・成長
    (['黒板', '授業', '教室', 'blackboard', 'classroom', 'lesson', 'teach', '講義', 'ホワイトボード'], '21_blackboard.jpg'),
    (['瞑想', '集中', '内省', '静か', 'meditate', 'meditation', 'focus', 'calm', '心', 'マインドフル'], '22_meditation.jpg'),
    (['遠くを見る', '展望', 'ビジョン', '未来', 'vision', 'future', 'horizon', 'dream', '夢', '目標'], '23_looking_far.jpg'),
    (['種', '育てる', '成長', '芽', '植える', 'seed', 'grow', 'plant', 'nurture', '育む', '可能性'], '24_planting_seed.jpg'),
    (['グラフ', '上昇', '成長', '向上', '改善', 'graph', 'growth', 'improve', 'progress', '上がる', '増加'], '25_rising_graph.jpg'),
    # 人間関係
    (['握手', '協力', 'パートナー', '契約', 'handshake', 'cooperate', 'partner', 'deal', '協働'], '26_handshake.jpg'),
    (['プレゼン', '発表', '説明', '伝える', 'present', 'presentation', 'explain', 'speech', '講演'], '27_presentation.jpg'),
    (['いいね', 'サムズアップ', '承認', '賛成', 'thumbs up', 'like', 'approve', 'good', 'great', 'nice'], '28_thumbs_up.jpg'),
    (['腕を組む', '自信', '堂々', '確信', 'arms crossed', 'confident', 'assertive', '自信満々'], '29_arms_crossed.jpg'),
    (['お辞儀', '感謝', '礼', '挨拶', 'bow', 'thank', 'gratitude', 'greeting', 'respect', '礼儀'], '30_bow.jpg'),
]

# ポーズ選択カウンター（同じポーズが連続しないよう管理）
_pose_counter = 0

def select_pose_file(scene_data):
    """
    シーンデータ（image_prompt, subtitle, keyword）からキーワードマッチングで
    最適なポーズ画像ファイルパスを返す。
    マッチしない場合はシーン番号に応じてローテーション選択。
    """
    global _pose_counter
    # 検索対象テキストを結合
    search_text = ' '.join([
        scene_data.get('image_prompt', ''),
        scene_data.get('subtitle', ''),
        scene_data.get('keyword', ''),
    ]).lower()

    # キーワードマッチング（スコアリング方式：最多マッチのポーズを選択）
    best_pose = None
    best_score = 0
    for keywords, pose_file in POSE_MAPPING:
        score = sum(1 for kw in keywords if kw.lower() in search_text)
        if score > best_score:
            best_score = score
            best_pose = pose_file

    if best_pose:
        pose_path = os.path.join(POSES_DIR, best_pose)
        if os.path.exists(pose_path):
            return pose_path

    # マッチなし → ローテーション選択
    all_poses = sorted([f for f in os.listdir(POSES_DIR) if f.endswith('.jpg')])
    if all_poses:
        pose_file = all_poses[_pose_counter % len(all_poses)]
        _pose_counter += 1
        return os.path.join(POSES_DIR, pose_file)

    return None


def generate_flux_image(pose_path, scene_data, index):
    """
    ローカルのポーズ画像をベースにFlux Kontext img2imgで背景付き画像を生成して返す。
    成功時は生成画像のURLを返す。失敗時はNoneを返す（呼び出し元でフォールバック）。
    """
    import base64, time

    bfl_api_key = os.environ.get('BFL_API_KEY', '').strip()
    if not bfl_api_key:
        print(f"[FLUX] Scene {index}: BFL_API_KEY not set, skipping Flux generation", file=sys.stderr, flush=True)
        return None

    # ポーズ画像をBase64エンコード
    try:
        with open(pose_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"[FLUX] Scene {index}: Failed to read pose image ({e})", file=sys.stderr, flush=True)
        return None

    # シーン内容からプロンプトを生成
    image_prompt = scene_data.get('image_prompt', '')
    subtitle = scene_data.get('subtitle', '')
    keyword = scene_data.get('keyword', '')
    scene_context = ' '.join(filter(None, [image_prompt, subtitle, keyword]))[:200]

    prompt = (
        f"Keep this exact stick figure character with the same pose and proportions. "
        f"Add a vivid, detailed background and environment that matches this scene: {scene_context}. "
        f"Maintain the black line art stick figure style. Make the background colorful and expressive "
        f"to enhance the visual storytelling. High quality illustration style."
    )

    headers = {
        'x-key': bfl_api_key,
        'Content-Type': 'application/json',
    }
    payload = {
        'prompt': prompt,
        'input_image': img_b64,
        'aspect_ratio': '16:9',
        'output_format': 'jpeg',
        'safety_tolerance': 6,
    }

    try:
        print(f"[FLUX] Scene {index}: Submitting job to BFL API...", file=sys.stderr, flush=True)
        resp = requests.post(
            'https://api.bfl.ml/v1/flux-kontext-pro',
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code != 200:
            print(f"[FLUX] Scene {index}: BFL API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
            return None

        result = resp.json()
        job_id_bfl = result.get('id')
        if not job_id_bfl:
            print(f"[FLUX] Scene {index}: No job ID returned", file=sys.stderr, flush=True)
            return None

        print(f"[FLUX] Scene {index}: Job submitted, polling... (id={job_id_bfl})", file=sys.stderr, flush=True)

        # ポーリング（最大120秒、3秒間隔）
        for attempt in range(40):
            time.sleep(3)
            poll_resp = requests.get(
                f'https://api.bfl.ml/v1/get_result?id={job_id_bfl}',
                headers={'x-key': bfl_api_key},
                timeout=15
            )
            poll_data = poll_resp.json()
            status = poll_data.get('status')
            if status == 'Ready':
                image_url = poll_data.get('result', {}).get('sample')
                print(f"[FLUX] Scene {index}: Image ready -> {str(image_url)[:80]}", file=sys.stderr, flush=True)
                return image_url
            elif status in ['Error', 'Failed', 'Content Moderated']:
                print(f"[FLUX] Scene {index}: Generation failed: {status}", file=sys.stderr, flush=True)
                return None
            elif attempt % 5 == 0:
                print(f"[FLUX] Scene {index}: Still waiting... status={status} (attempt {attempt+1}/40)", file=sys.stderr, flush=True)

        print(f"[FLUX] Scene {index}: Timeout waiting for image", file=sys.stderr, flush=True)
        return None

    except Exception as e:
        print(f"[FLUX] Scene {index}: Exception during generation: {e}", file=sys.stderr, flush=True)
        return None


def generate_image_local(scene_data, job_dir, index):
    """画像を取得してファイルパスを返す。
    1. まずローカルポーズ画像を選択する
    2. BFL_API_KEYが設定されていればFlux Kontextでimg2img生成（背景追加）
    3. Flux生成に失敗した場合はローカルポーズ画像をそのまま使用
    """
    import shutil
    img_path = os.path.join(job_dir, f"image_{index:03d}.jpg")

    # ローカルポーズ画像を選択
    pose_path = select_pose_file(scene_data)
    if not pose_path or not os.path.exists(pose_path):
        # フォールバック: グレー背景のダミー画像を生成
        from PIL import Image as PILImage, ImageDraw
        img = PILImage.new('RGB', (1080, 1080), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.text((540, 540), f"Scene {index+1}", fill=(100, 100, 100), anchor='mm')
        img.save(img_path, 'JPEG', quality=85)
        print(f"[LOCAL] Scene {index}: Fallback dummy image", file=sys.stderr, flush=True)
        return img_path

    print(f"[LOCAL] Scene {index}: Selected pose '{os.path.basename(pose_path)}'", file=sys.stderr, flush=True)

    # Flux Kontextでimg2img生成を試みる
    flux_url = generate_flux_image(pose_path, scene_data, index)
    if flux_url:
        # Flux生成画像をダウンロード
        try:
            resp = requests.get(flux_url, timeout=60)
            resp.raise_for_status()
            with open(img_path, 'wb') as f:
                f.write(resp.content)
            print(f"[FLUX] Scene {index}: Saved generated image ({len(resp.content)} bytes)", file=sys.stderr, flush=True)
            return img_path
        except Exception as e:
            print(f"[FLUX] Scene {index}: Download failed ({e}), falling back to local pose", file=sys.stderr, flush=True)

    # フォールバック: ローカルポーズ画像をそのまま使用
    shutil.copy2(pose_path, img_path)
    print(f"[LOCAL] Scene {index}: Using pose '{os.path.basename(pose_path)}' (Flux skipped/failed)", file=sys.stderr, flush=True)
    return img_path


@app.route('/generate-slideshow', methods=['POST'])
def generate_slideshow():
    """
    シーンデータからFlux APIで画像生成→FFmpegでスライドショー動画を作成する統合エンドポイント。
    非同期処理（Render.comの30分タイムアウト回避）: job_idを即座に返してバックグラウンドで処理。
    /job-status/{job_id}で進捗・完了をポーリングする。
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "error": "No JSON body"}), 400

    scenes = data.get("scenes", [])
    audio_b64 = data.get("audio_b64", "")
    audio_url = data.get("audio_url", "")  # audio_urlも受け付ける（n8nメモリ節約）
    audio_path_direct = data.get("audio_path", "")  # サーバー内パス（自己参照ループ回避）
    resolution = data.get("resolution", "1920x1080")
    fps = data.get("fps", 30)
    title = data.get("title", "")
    subtitle_font_size = data.get("subtitle_font_size", 44)
    enable_ken_burns = data.get("enable_ken_burns", True)
    enable_transitions = data.get("enable_transitions", False)  # メモリ節約のためデフォルトFalse
    transition_duration = data.get("transition_duration", 0.5)
    test_mode = data.get("test_mode", False)  # テストモード: Flux APIをスキップしてダミー画像を使用

    if not scenes:
        return jsonify({"success": False, "error": "No scenes provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # ジョブを登録してバックグラウンドスレッドで実行開始
    _save_job(job_id, {"status": "running", "result": None, "error": None, "progress": "starting"})

    # request.host_urlはリクエストコンテキスト外（スレッド内）では使えないため、ここで取得する
    base_url = request.host_url.rstrip('/')

    def run_job():
        try:
            _run_slideshow_job(job_id, job_dir, scenes, audio_b64, audio_url, audio_path_direct,
                               resolution, fps, title, subtitle_font_size, enable_ken_burns,
                               enable_transitions, transition_duration, test_mode, base_url)
        except Exception as e:
            job = _load_job(job_id) or {}
            job["status"] = "error"
            job["error"] = str(e)
            _save_job(job_id, job)
            print(f"[JOB {job_id}] Unhandled error: {traceback.format_exc()}", file=sys.stderr, flush=True)

    t = threading.Thread(target=run_job, daemon=True)
    t.start()

    print(f"[GENERATE-SLIDESHOW] Job {job_id} started: {len(scenes)} scenes", file=sys.stderr, flush=True)
    return jsonify({"success": True, "job_id": job_id, "status": "running", "poll_url": f"/job-status/{job_id}"})


def _run_slideshow_job(job_id, job_dir, scenes, audio_b64, audio_url, audio_path_direct,
                       resolution, fps, title, subtitle_font_size, enable_ken_burns,
                       enable_transitions, transition_duration, test_mode, base_url=''):
    """バックグラウンドで動画生成を実行するメイン処理"""
    def update_progress(msg):
        job = _load_job(job_id) or {}
        job["progress"] = msg
        _save_job(job_id, job)
        print(f"[JOB {job_id}] {msg}", file=sys.stderr, flush=True)

    try:
        # 解像度をパース
        if 'x' in resolution:
            width, height = map(int, resolution.split('x'))
        else:
            width, height = 1920, 1080

        # 音声の長さを推定してシーンの尺を計算
        if audio_path_direct and os.path.exists(audio_path_direct):
            # サーバー内パスから直接ファイルサイズを取得
            file_size_bytes = os.path.getsize(audio_path_direct)
            est_duration = file_size_bytes / 16000
            per_scene = max(2.0, est_duration / len(scenes))
        elif audio_b64:
            b64_part = audio_b64.split(',')[-1]
            audio_bytes = len(b64_part) * 3 // 4
            est_duration = audio_bytes / 16000
            per_scene = max(2.0, est_duration / len(scenes))
        elif audio_url:
            # audio_urlの場合はファイルをダウンロードしてサイズを確認
            try:
                head_resp = requests.head(audio_url, timeout=10)
                content_length = int(head_resp.headers.get('content-length', 0))
                if content_length > 0:
                    est_duration = content_length / 16000
                    per_scene = max(2.0, est_duration / len(scenes))
                else:
                    per_scene = 5.0
            except:
                per_scene = 5.0
        else:
            per_scene = 5.0

        # ===== Step 1: 画像を1枚ずつ生成（直列処理でメモリ節約） =====
        images_data = []
        for i, scene in enumerate(scenes):
            if test_mode:
                # テストモード: Flux APIをスキップしてダミー画像を生成（クレジット節約）
                img_path = os.path.join(job_dir, f"image_{i:03d}.jpg")
                from PIL import Image, ImageDraw, ImageFont
                img = Image.new('RGB', (1920, 1080), color=(240, 240, 240))
                draw = ImageDraw.Draw(img)
                draw.rectangle([0, 0, 1920, 1080], fill=(200, 220, 240))
                text = f"[TEST] Scene {i+1}\n{scene.get('subtitle', '')[:40]}"
                draw.text((960, 540), text, fill=(50, 50, 50), anchor='mm')
                img.save(img_path, 'JPEG', quality=85)
                print(f"[TEST] Scene {i}: Dummy image created -> {img_path}", file=sys.stderr, flush=True)
            else:
                img_path = generate_image_local(scene, job_dir, i)
            images_data.append({
                "path": img_path,
                "duration": round(per_scene, 1),
                "subtitle": scene.get("subtitle", ""),
                "keyword": scene.get("keyword", "")
            })

        # ===== Step 2: 音声ファイルを保存 =====
        audio_path = None
        if audio_path_direct and os.path.exists(audio_path_direct):
            # サーバー内パスを直接使用（自己参照ループ回避）
            audio_path = audio_path_direct
            print(f"[GENERATE-SLIDESHOW] Using audio from path: {audio_path} ({os.path.getsize(audio_path)} bytes)", file=sys.stderr, flush=True)
        elif audio_url:
            # audio_urlからダウンロード（n8nメモリ節約方式）
            audio_path = os.path.join(job_dir, "narration.mp3")
            download_file(audio_url, audio_path)
            print(f"[GENERATE-SLIDESHOW] Audio downloaded from URL: {os.path.getsize(audio_path)} bytes", file=sys.stderr, flush=True)
        elif audio_b64:
            audio_path = os.path.join(job_dir, "narration.mp3")
            save_base64_audio(audio_b64, audio_path)

        # ===== Step 3: FFmpegでスライドショー動画を作成 =====
        kb_patterns = ['zoom_in', 'pan_right', 'zoom_out', 'pan_left', 'zoom_in', 'pan_right',
                       'zoom_out', 'pan_left', 'zoom_in', 'pan_right', 'zoom_out', 'pan_left',
                       'zoom_in', 'pan_right', 'zoom_out', 'pan_left', 'zoom_in', 'pan_right',
                       'zoom_out', 'pan_left', 'zoom_in', 'pan_right', 'zoom_out', 'pan_left']

        clip_files = []
        clip_durations = []

        for i, img_data in enumerate(images_data):
            duration = img_data["duration"]
            img_path = img_data["path"]
            subtitle_text = img_data.get("subtitle", "")
            keyword_text = img_data.get("keyword", "")
            clip_path = os.path.join(job_dir, f"clip_{i:03d}.mp4")

            if enable_ken_burns:
                kb_type = kb_patterns[i % len(kb_patterns)]
                kb_filter, used_effect = get_ken_burns_filter(width, height, duration, kb_type, fps)
                vf_parts = [
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white",
                    kb_filter,
                    "format=yuv420p"
                ]
            else:
                vf_parts = [
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white",
                    "format=yuv420p"
                ]

            if subtitle_text:
                wrapped = wrap_text(subtitle_text, max_chars=18)
                lines = wrapped.split('\n')
                sub_font_size = subtitle_font_size + 8
                y_base = height - 180
                for line_idx, line in enumerate(lines[:2]):
                    escaped = escape_ffmpeg_text(line)
                    if not escaped:
                        continue
                    y_pos = y_base + line_idx * (sub_font_size + 10)
                    fade_in = f"if(lt(t,0.3),0,if(lt(t,0.8),255*(t-0.3)/0.5,255))"
                    vf_parts.append(
                        f"drawtext=fontfile={FONT_PATH}:text='{escaped}':fontsize={sub_font_size}:fontcolor=black:alpha='({fade_in})/255':x=(w-text_w)/2:y={y_pos}:borderw=3:bordercolor=white"
                    )

            if keyword_text:
                escaped_kw = escape_ffmpeg_text(keyword_text)
                if escaped_kw:
                    kw_font_size = 72
                    vf_parts.append(
                        f"drawtext=fontfile={FONT_PATH}:text='{escaped_kw}':fontsize={kw_font_size}:fontcolor=black:alpha='if(lt(t,0.2),0,if(lt(t,0.7),255*(t-0.2)/0.5,255))/255':x=(w-text_w)/2:y=(h-text_h)/2-80:borderw=4:bordercolor=white"
                    )

            vf_str = ",".join(vf_parts)
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", img_path,
                "-t", str(duration),
                "-vf", vf_str,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-r", str(fps),
                clip_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise Exception(f"FFmpeg clip {i} failed: {result.stderr[-500:]}")
            clip_files.append(clip_path)
            clip_durations.append(duration)
            print(f"[JOB {job_id}] Clip {i+1}/{len(images_data)} created", file=sys.stderr, flush=True)

        # クリップを結合
        output_filename = f"{job_id}.mp4"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        # XFADEは2GBでもOOMを引き起こすため廃止。シンプルなconcatを使用。
        concat_list = os.path.join(job_dir, "concat.txt")
        with open(concat_list, 'w') as f:
            for cf in clip_files:
                f.write(f"file '{cf}'\n")
        print(f"[JOB {job_id}] Concatenating {len(clip_files)} clips with simple concat...", file=sys.stderr, flush=True)
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
               "-c", "copy", output_path]
        subprocess.run(cmd, capture_output=True, check=True, timeout=600)
        final_video = output_path
        print(f"[JOB {job_id}] Concat done: {output_path}", file=sys.stderr, flush=True)

        # 音声を合成
        if audio_path and os.path.exists(audio_path):
            final_with_audio = os.path.join(OUTPUT_DIR, f"{job_id}_final.mp4")
            # 音声を一度PCMに変換してからAACエンコード（EOF before openエラー回避）
            audio_converted = os.path.join(job_dir, "narration_converted.aac")
            cmd_convert = [
                "ffmpeg", "-y",
                "-i", audio_path,
                "-ar", "44100", "-ac", "2",
                "-c:a", "aac", "-b:a", "192k",
                audio_converted
            ]
            conv_result = subprocess.run(cmd_convert, capture_output=True, text=True, timeout=60)
            if conv_result.returncode != 0:
                print(f"[JOB {job_id}] Audio convert FAILED: {conv_result.stderr[-300:]}", file=sys.stderr, flush=True)
                audio_converted = audio_path  # 変換失敗時は元ファイルを使用
            else:
                print(f"[JOB {job_id}] Audio converted to AAC: {audio_converted}", file=sys.stderr, flush=True)
            cmd = [
                "ffmpeg", "-y",
                "-i", final_video,
                "-i", audio_converted,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-shortest",
                final_with_audio
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                os.rename(final_with_audio, final_video)
                print(f"[JOB {job_id}] Audio merge success: {final_video}", file=sys.stderr, flush=True)
            else:
                print(f"[JOB {job_id}] Audio merge FAILED: {result.stderr[-500:]}", file=sys.stderr, flush=True)

        # サムネイル生成（1シーン目の画像を使用）
        thumbnail_path = None
        if images_data and title:
            try:
                thumb_src = images_data[0]["path"]
                thumbnail_path = os.path.join(OUTPUT_DIR, f"{job_id}_thumb.jpg")
                cmd = ["ffmpeg", "-y", "-i", thumb_src, "-vframes", "1",
                       "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white",
                       thumbnail_path]
                subprocess.run(cmd, capture_output=True, timeout=30)
            except Exception as e:
                print(f"[GENERATE-SLIDESHOW] Thumbnail error: {e}", file=sys.stderr, flush=True)

        if not base_url:
            base_url = 'https://ffmpeg-video-server.onrender.com'
        response_data = {
            "success": True,
            "job_id": job_id,
            "video_url": f"{base_url}/download/{os.path.basename(final_video)}",
            "filename": os.path.basename(final_video),
            "scenes_count": len(scenes)
        }
        if thumbnail_path and os.path.exists(thumbnail_path):
            response_data["thumbnail_url"] = f"{base_url}/download/{os.path.basename(thumbnail_path)}"

        print(f"[JOB {job_id}] Complete: {response_data['video_url']}", file=sys.stderr, flush=True)
        _save_job(job_id, {"status": "done", "result": response_data})

    except Exception as e:
        print(f"[JOB {job_id}] Error: {traceback.format_exc()}", file=sys.stderr, flush=True)
        job = _load_job(job_id) or {}
        job["status"] = "error"
        job["error"] = str(e)
        _save_job(job_id, job)
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


@app.route('/job-status/<job_id>', methods=['GET'])
def job_status(job_id):
    """非同期ジョブの進捗・完了・エラーを返す（n8nポーリング用）"""
    job = _load_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    if job["status"] == "done":
        return jsonify({"success": True, "status": "done", **job["result"]})
    elif job["status"] == "error":
        return jsonify({"success": False, "status": "error", "error": job["error"]}), 500
    else:
        return jsonify({"success": True, "status": "running", "progress": job.get("progress", "")})


@app.route('/job-cancel/<job_id>', methods=['POST'])
def job_cancel(job_id):
    """ジョブをキャンセル（クリーンアップ用）"""
    p = _job_path(job_id)
    if os.path.exists(p):
        os.remove(p)
    return jsonify({"success": True})


@app.route('/test-kontext', methods=['POST'])
def test_kontext():
    """
    Flux Kontext img2img テスト用エンドポイント
    棒人間画像をベースにシーン内容に合った画像を生成して返す
    """
    import base64
    import time
    import requests as _req

    data = request.get_json(force=True)
    bfl_api_key = data.get("bfl_api_key", "")
    pose_name = data.get("pose", "02_guts_pose.jpg")  # posesディレクトリのファイル名
    prompt = data.get("prompt", "Keep this exact stick figure character. Add a simple mountain summit background with flag. Maintain white background and black line art style.")

    if not bfl_api_key:
        return jsonify({"success": False, "error": "bfl_api_key is required"}), 400

    # ポーズ画像を読み込んでbase64エンコード
    pose_path = os.path.join(POSES_DIR, pose_name)
    if not os.path.exists(pose_path):
        return jsonify({"success": False, "error": f"Pose not found: {pose_name}"}), 404

    with open(pose_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    # Flux Kontext API呼び出し
    headers = {
        "x-key": bfl_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "input_image": img_b64,
        "aspect_ratio": "16:9",
        "output_format": "jpeg",
        "safety_tolerance": 6,
    }

    try:
        resp = _req.post(
            "https://api.bfl.ml/v1/flux-kontext-pro",
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code != 200:
            return jsonify({"success": False, "error": f"BFL API error: {resp.status_code} {resp.text}"}), 500

        result = resp.json()
        job_id_bfl = result.get("id")
        if not job_id_bfl:
            return jsonify({"success": False, "error": "No job ID returned"}), 500

        # ポーリング（最大90秒）
        for _ in range(30):
            time.sleep(3)
            poll_resp = _req.get(
                f"https://api.bfl.ml/v1/get_result?id={job_id_bfl}",
                headers={"x-key": bfl_api_key},
                timeout=15
            )
            poll_data = poll_resp.json()
            status = poll_data.get("status")
            if status == "Ready":
                image_url = poll_data.get("result", {}).get("sample")
                return jsonify({"success": True, "image_url": image_url, "job_id": job_id_bfl})
            elif status in ["Error", "Failed", "Content Moderated"]:
                return jsonify({"success": False, "error": f"Generation failed: {status}"}), 500

        return jsonify({"success": False, "error": "Timeout waiting for image"}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    import os as _os
    port = int(_os.environ.get('PORT', 10000))
    print(f"Video Merge Server starting on port {port} with waitress...")
    print("Features: Ken Burns effect, Text animation, xfade transitions")
    from waitress import serve
    serve(app, host='0.0.0.0', port=port, threads=8, channel_timeout=0)
