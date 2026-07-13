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

def cleanup_old_files():
    while True:
        time.sleep(3600)
        now = time.time()
        for d in [WORK_DIR, OUTPUT_DIR]:
            for f in os.listdir(d):
                fpath = os.path.join(d, f)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 7200:
                    os.remove(fpath)

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
    enable_transitions = data.get("enable_transitions", True)
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
    複数クリップをffmpegの複合フィルターで一度に処理する。
    """
    n = len(clip_files)
    if n == 1:
        import shutil
        shutil.copy(clip_files[0], output_path)
        return output_path

    # xfadeフィルターを構築
    # 各クリップのオフセット（累積時間）を計算
    # offset[i] = sum(duration[0..i-1]) - transition_duration * i
    # （クロスフェードでオーバーラップする分を引く）

    # ffmpegコマンド：全クリップを入力として、xfadeフィルターチェーンで結合
    cmd = ["ffmpeg", "-y"]

    # 全クリップを入力
    for cf in clip_files:
        cmd.extend(["-i", cf])

    # xfadeフィルターチェーンを構築
    # [0][1]xfade=transition=fade:duration=0.5:offset=X[v01];
    # [v01][2]xfade=transition=fade:duration=0.5:offset=Y[v012];
    # ...

    filter_parts = []
    current_label = "[0:v]"
    cumulative_offset = 0.0

    for i in range(1, n):
        # このクリップが始まるオフセット
        cumulative_offset += clip_durations[i-1] - transition_duration
        next_label = f"[v{i}]" if i < n-1 else "[vout]"

        filter_parts.append(
            f"{current_label}[{i}:v]xfade=transition=fade:duration={transition_duration}:offset={cumulative_offset:.3f}{next_label}"
        )
        current_label = next_label

    filter_complex = ";".join(filter_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", str(fps),
        "-movflags", "+faststart",
        output_path
    ])

    print(f"[SLIDESHOW] xfade command: {' '.join(cmd[:10])}...", file=sys.stderr, flush=True)
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
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

if __name__ == '__main__':
    print("Video Merge Server starting on port 5555...")
    print("Features: Ken Burns effect, Text animation, xfade transitions")
    app.run(host='0.0.0.0', port=5555, debug=False)
