"""
Pipeline completo de edição de Reels.

1. Download video
2. Converter VFR → CFR
3. Remover silêncios
4. Transcrever (Whisper)
5. Analisar segmentos + overlay plan (AI)
6. Gerar imagens hook (Gemini)
6b. Gerar 8+ overlay images (Gemini)
7. Gerar vídeos Sora (opcional)
7b. Gerar SFX pop
8. Construir hook frames (PIL)
9. Editar vídeo (zoom + hook + Ken Burns + flash/shake transitions)
9b. Aplicar image overlays (blur_overlay / split)
9c. Build SFX track
10. Captions karaokê (ASS)
11. Burn captions + 3-audio mix (voz + música + SFX)
12. Upload pro Storage
"""

import os
import json
import math
import time
import shutil
import tempfile
import subprocess
import requests
from PIL import Image, ImageDraw, ImageFont
from typing import Callable
from lib.supabase_client import upload_to_storage, delete_from_storage, SUPABASE_URL

MUSIC_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "music", "epic_games.mp3")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Impact bundled no repo > fallbacks do sistema
_BUNDLED_IMPACT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts", "Impact.ttf")
for fp in [
    _BUNDLED_IMPACT,                                          # bundled (Railway + local)
    "/System/Library/Fonts/Supplemental/Impact.ttf",          # macOS system
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux fallback
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]:
    if os.path.exists(fp):
        FONT_PATH = fp
        break


def run_pipeline(
    video_url: str,
    user_id: str,
    openai_key: str,
    gemini_key: str,
    hook_line1: str | None = None,
    hook_line2: str | None = None,
    zoom_levels: list | None = None,
    generate_sora: bool = True,
    progress_callback: Callable | None = None,
) -> dict:

    if zoom_levels is None:
        zoom_levels = [1.0, 1.5, 1.0, 1.6]

    import sys

    def progress(pct, step):
        if progress_callback:
            progress_callback(pct, step)
        print(f"[REELS] {pct}% — {step}", flush=True)

    # Use /app/workdata instead of /tmp to avoid tmpfs (RAM) space issues
    base_workdir = os.environ.get("REELS_WORKDIR", "/app/workdata")
    os.makedirs(base_workdir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="reels_", dir=base_workdir)
    print(f"[REELS] Workdir: {workdir}", flush=True)

    try:
        # ===== 1. DOWNLOAD VIDEO =====
        progress(5, "downloading_video")
        video_path = os.path.join(workdir, "bruto.mp4")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(video_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"[REELS] Downloaded: {os.path.getsize(video_path)} bytes", flush=True)

        # Obter resolução
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
            capture_output=True, text=True
        )
        streams = json.loads(probe.stdout)["streams"]
        vstream = next(s for s in streams if s["codec_type"] == "video")
        W, H = int(vstream["width"]), int(vstream["height"])
        print(f"[REELS] Resolution: {W}x{H}", flush=True)

        # ===== 2. CONVERT VFR → CFR =====
        progress(8, "converting_cfr")
        cfr_path = os.path.join(workdir, "bruto_cfr.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", "fps=30", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k", "-video_track_timescale", "30000",
            cfr_path
        ], capture_output=True, check=True)

        # ===== 3. REMOVE SILENCES =====
        progress(12, "removing_silences")
        nosilence_path = os.path.join(workdir, "no_silence.mp4")
        try:
            subprocess.run(
                ["auto-editor", cfr_path, "--margin", "0.15s", "-o", nosilence_path],
                capture_output=True, check=True, timeout=300
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Se auto-editor não está instalado, pular
            print("[REELS] auto-editor not available, skipping silence removal", flush=True)
            shutil.copy(cfr_path, nosilence_path)

        # ===== 4. TRANSCRIBE (WHISPER) =====
        progress(18, "transcribing")
        transcription = transcribe_whisper(nosilence_path, openai_key, workdir)
        words = transcription.get("words", [])
        full_text = transcription.get("text", "")
        print(f"[REELS] Transcribed: {len(words)} words, {len(full_text)} chars", flush=True)

        # ===== 5. ANALYZE + PLAN (now includes overlay_images) =====
        progress(22, "analyzing_content")
        plan = analyze_content(full_text, openai_key, hook_line1, hook_line2)
        hook_l1 = plan["hook_line1"]
        hook_l2 = plan["hook_line2"]
        segments = plan["segments"]
        hook_images_prompts = plan["hook_images"]
        sora_prompts = plan["sora_videos"]
        overlay_specs = plan.get("overlay_images", [])

        # Hard limit: max 85s de segmentos (video final ~90s com hook)
        HOOK_DUR = 5.0
        MAX_CONTENT_DUR = 85.0
        trimmed_segments = []
        total_dur = 0.0
        for seg in segments:
            s = seg["start"] if isinstance(seg, dict) else seg[0]
            e = seg["end"] if isinstance(seg, dict) else seg[1]
            # Segment 0: edit_video skips content before HOOK_DUR
            effective_s = max(s, HOOK_DUR) if len(trimmed_segments) == 0 else s
            seg_dur = e - effective_s
            if seg_dur <= 0:
                continue
            if total_dur + seg_dur > MAX_CONTENT_DUR:
                remaining = MAX_CONTENT_DUR - total_dur
                if remaining > 2:  # so inclui se sobrar mais de 2s
                    if isinstance(seg, dict):
                        seg = {**seg, "end": effective_s + remaining}
                    trimmed_segments.append(seg)
                break
            trimmed_segments.append(seg)
            total_dur += seg_dur
        if len(trimmed_segments) < len(segments):
            print(f"[REELS] Trimmed segments: {len(segments)} -> {len(trimmed_segments)} (max {MAX_CONTENT_DUR}s)", flush=True)
        segments = trimmed_segments

        # Build timeline map: original timestamps -> edited video timestamps
        remap_ts = build_timeline_map(segments, HOOK_DUR)

        print(f"[REELS] Plan: hook='{hook_l1}/{hook_l2}', {len(segments)} segments, {len(sora_prompts)} sora, {len(overlay_specs)} overlays", flush=True)

        # Filter overlay collisions with Sora windows
        if overlay_specs and sora_prompts:
            overlay_specs = filter_overlay_collisions(overlay_specs, sora_prompts)
            print(f"[REELS] After collision filter: {len(overlay_specs)} overlays", flush=True)

        # ===== 6. GENERATE HOOK IMAGES (GEMINI) =====
        progress(28, "generating_hook_images")
        hook_img_a = generate_gemini_image(hook_images_prompts[0], gemini_key, workdir, "hook_a.png")
        time.sleep(2)  # Gemini rate limit
        hook_img_b = generate_gemini_image(hook_images_prompts[1], gemini_key, workdir, "hook_b.png")

        # ===== 6b. GENERATE OVERLAY IMAGES (GEMINI) =====
        overlay_data = []
        if overlay_specs:
            progress(32, "generating_overlay_images")
            overlay_data = generate_overlay_images(overlay_specs, gemini_key, workdir)
            print(f"[REELS] Generated {len(overlay_data)} overlay images", flush=True)
            # Remap overlay timestamps from original to edited timeline
            remapped_ov = []
            for ov in overlay_data:
                new_t = remap_ts(ov["insert_at"])
                if new_t is not None:
                    ov["insert_at"] = new_t
                    remapped_ov.append(ov)
            overlay_data = remapped_ov
            print(f"[REELS] Remapped {len(overlay_data)} overlays to edited timeline", flush=True)

        # ===== 7. GENERATE SORA VIDEOS (OPTIONAL) =====
        sora_paths = []
        if generate_sora and sora_prompts:
            progress(42, "generating_sora_videos")
            sora_paths = generate_sora_videos(sora_prompts, openai_key, workdir, W, H)
            # Remap Sora insert_at from original to edited timeline
            for sp in sora_paths:
                new_t = remap_ts(sp["insert_at"])
                if new_t is not None:
                    sp["insert_at"] = new_t
                else:
                    sp["insert_at"] = -1
            sora_paths = [sp for sp in sora_paths if sp["insert_at"] >= 0]
            print(f"[REELS] Remapped {len(sora_paths)} Sora cutaways to edited timeline", flush=True)
            progress(55, "sora_videos_done")
        else:
            progress(55, "skipping_sora")

        # ===== 7b. GENERATE SFX POP =====
        progress(55, "generating_sfx")
        try:
            sfx_pop_path = generate_sfx_pop(workdir)
            print(f"[REELS] SFX pop generated", flush=True)
        except Exception as e:
            sfx_pop_path = None
            print(f"[REELS] SFX pop generation failed: {e}", flush=True)

        # ===== 8. BUILD HOOK FRAMES =====
        progress(58, "building_hook_frames")
        hook_frame_a, hook_frame_b, video_start_y, crop_top = build_hook_frames(
            W, H, hook_img_a, hook_img_b, hook_l1, hook_l2, nosilence_path, workdir
        )

        # ===== 9. EDIT VIDEO (zoom + hook + Ken Burns + transitions) =====
        progress(62, "editing_video")
        noCaption_path = edit_video(
            nosilence_path, W, H, hook_frame_a, hook_frame_b,
            video_start_y, crop_top, segments, zoom_levels, sora_paths, workdir
        )

        # Critical check: edit_video MUST produce a valid file
        if not os.path.exists(noCaption_path) or os.path.getsize(noCaption_path) < 1000:
            raise RuntimeError(f"edit_video failed to produce output at {noCaption_path}")
        print(f"[REELS] edit_video OK: {os.path.getsize(noCaption_path)} bytes", flush=True)
        progress(72, "video_edited")

        # ===== 9b. APPLY IMAGE OVERLAYS =====
        # Save a backup of the video before overlays (they can fail and corrupt)
        pre_overlay_path = noCaption_path
        if overlay_data:
            progress(72, "applying_image_overlays")
            try:
                import shutil
                backup_path = os.path.join(workdir, "reels_noCaption_backup.mp4")
                shutil.copy2(noCaption_path, backup_path)
                noCaption_path = apply_image_overlays(noCaption_path, overlay_data, W, H, workdir)
                # Verify the result file exists and is valid
                if not os.path.exists(noCaption_path) or os.path.getsize(noCaption_path) < 1000:
                    print(f"[REELS] Overlay result missing or too small, using backup", flush=True)
                    noCaption_path = backup_path
                else:
                    print(f"[REELS] Image overlays applied successfully", flush=True)
            except Exception as e:
                print(f"[REELS] Overlay step failed ({e}), continuing without overlays", flush=True)
                noCaption_path = backup_path if os.path.exists(backup_path) else pre_overlay_path

        # Verify video file exists before continuing
        if not os.path.exists(noCaption_path):
            raise RuntimeError(f"Video file missing after overlay step: {noCaption_path}")

        # ===== 9c. BUILD SFX TRACK =====
        sfx_track_path = None
        if sfx_pop_path:
            progress(78, "building_sfx_track")
            try:
                sfx_timestamps = collect_sfx_timestamps(segments, overlay_data, HOOK_DUR)
                total_dur = get_duration(noCaption_path)
                sfx_track_path = build_sfx_track(sfx_pop_path, sfx_timestamps, total_dur, workdir)
            except Exception as e:
                print(f"[REELS] SFX track failed ({e}), continuing without SFX", flush=True)
                sfx_track_path = None

        # ===== 10. CAPTIONS (ASS) =====
        progress(80, "generating_captions")
        print(f"[REELS] noCaption file: {os.path.getsize(noCaption_path)} bytes", flush=True)
        ass_path = generate_captions(noCaption_path, openai_key, W, H, workdir)
        print(f"[REELS] ASS captions generated", flush=True)

        # Limpar segmentos intermediários pra liberar disco
        for f_name in os.listdir(workdir):
            f_path = os.path.join(workdir, f_name)
            if f_name.startswith("seg_") or f_name.startswith("hook_") or f_name == "hook.mp4":
                try:
                    os.remove(f_path)
                except:
                    pass

        # ===== 11. BURN CAPTIONS + 3-AUDIO MIX =====
        progress(85, "burning_captions")
        print("[REELS] Starting burn_captions_and_music (3-audio mix)...", flush=True)
        final_path = burn_captions_and_music(noCaption_path, ass_path, workdir, sfx_track_path=sfx_track_path)
        print(f"[REELS] Final video: {os.path.getsize(final_path)} bytes", flush=True)
        progress(92, "video_finalized")

        # Limpar tudo exceto o final
        for f_name in os.listdir(workdir):
            f_path = os.path.join(workdir, f_name)
            if f_path != final_path and os.path.isfile(f_path):
                try:
                    os.remove(f_path)
                except:
                    pass

        # ===== 12. UPLOAD TO STORAGE =====
        progress(95, "uploading")
        file_size = os.path.getsize(final_path)
        file_size_mb = round(file_size / 1024 / 1024, 1)
        print(f"[REELS] Uploading {file_size_mb}MB to Storage...", flush=True)
        storage_path = f"output/{user_id}/{os.path.basename(workdir)}/REELS_FINAL.mp4"

        try:
            public_url = upload_to_storage("reels", storage_path, final_path)
        except Exception as upload_err:
            if "413" in str(upload_err) or "too large" in str(upload_err).lower() or "Payload" in str(upload_err):
                # Re-encode com bitrate menor e tentar de novo
                print(f"[REELS] Upload falhou ({file_size_mb}MB). Re-encoding com CRF 28...", flush=True)
                smaller_path = os.path.join(workdir, "REELS_FINAL_small.mp4")
                subprocess.run([
                    "ffmpeg", "-y", "-i", final_path,
                    "-c:v", "libx264", "-crf", "28", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "128k", smaller_path
                ], capture_output=True)
                if os.path.exists(smaller_path):
                    final_path = smaller_path
                    file_size_mb = round(os.path.getsize(final_path) / 1024 / 1024, 1)
                    print(f"[REELS] Re-encoded: {file_size_mb}MB. Retentando upload...", flush=True)
                    public_url = upload_to_storage("reels", storage_path, final_path)
                else:
                    raise upload_err
            else:
                raise

        print(f"[REELS] Upload done: {public_url}", flush=True)

        # ===== 12b. DELETE RAW UPLOAD FROM STORAGE =====
        # If video_url is from our Storage (uploads/), delete it to save space
        storage_prefix = f"{SUPABASE_URL}/storage/v1/object/public/reels/uploads/"
        if video_url.startswith(storage_prefix):
            raw_path = video_url.replace(f"{SUPABASE_URL}/storage/v1/object/public/reels/", "")
            try:
                deleted = delete_from_storage("reels", [raw_path])
                if deleted:
                    print(f"[REELS] Raw upload deleted: {raw_path}", flush=True)
                else:
                    print(f"[REELS] Failed to delete raw upload: {raw_path}", flush=True)
            except Exception as e:
                print(f"[REELS] Error deleting raw upload: {e}", flush=True)

        progress(100, "done")

        return {
            "video_url": public_url,
            "duration": get_duration(final_path),
            "resolution": f"{W}x{H}",
            "hook_text": f"{hook_l1}\n{hook_l2}",
            "transcript": full_text[:500],
        }

    finally:
        # Cleanup workdir
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except:
            pass


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def get_duration(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True
    )
    try:
        data = json.loads(probe.stdout)
    except (json.JSONDecodeError, ValueError):
        print(f"[REELS] get_duration: ffprobe returned no JSON for {path}", flush=True)
        return 60.0  # fallback

    # Try format.duration first
    if "format" in data and "duration" in data["format"]:
        return float(data["format"]["duration"])

    # Fallback: try first stream with duration
    for stream in data.get("streams", []):
        if "duration" in stream:
            return float(stream["duration"])

    print(f"[REELS] get_duration: no duration found for {path}, using fallback", flush=True)
    return 60.0


def transcribe_whisper(video_path, openai_key, workdir):
    """Transcreve com Whisper API. Se > 25MB, extrai audio primeiro."""
    file_size = os.path.getsize(video_path)
    upload_path = video_path

    if file_size > 25 * 1024 * 1024:
        audio_path = os.path.join(workdir, "audio.m4a")
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "64k", audio_path
        ], capture_output=True, check=True)
        upload_path = audio_path

    with open(upload_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {openai_key}"},
            files={"file": (os.path.basename(upload_path), f)},
            data={
                "model": "whisper-1",
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
                "language": "pt",
            },
        )
    resp.raise_for_status()
    result = resp.json()

    with open(os.path.join(workdir, "transcription.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False)

    return result


def analyze_content(text, openai_key, hook_line1=None, hook_line2=None):
    """Usa GPT-4o-mini pra analisar transcrição e gerar plano de edição."""
    prompt = f"""Analise esta transcrição de um Reels e retorne um JSON com:
1. "hook_line1": primeira linha do hook (máximo 5 palavras, IMPACTANTE, CAPSLOCK)
2. "hook_line2": segunda linha do hook (máximo 6 palavras, IMPACTANTE, CAPSLOCK)
3. "segments": array de objetos {{"start": seconds, "end": seconds, "topic": "descrição curta"}}
   - Divida em 5-8 segmentos temáticos baseado nas mudanças de assunto
   - IMPORTANTE: duracao TOTAL de todos os segmentos deve ser no MAXIMO 85 segundos (video final sera ~90s com hook)
   - Se a transcricao for longa, selecione APENAS os trechos mais impactantes
4. "hook_images": array com 2 prompts em PORTUGUÊS para gerar imagens realistas 16:9
   - Estilo: foto real, luz natural, sem filtro, sem efeito cinematografico. NUNCA mencionar iPhone, camera ou dispositivo
   - Devem ser impactantes e complementares ao tema do vídeo
5. "sora_videos": array com 3 objetos {{"prompt": "em português", "insert_at": seconds}}
   - Videos de apoio (cutaway) estilo real, luz natural, sem filtros. NUNCA mencionar iPhone, camera ou dispositivo
   - insert_at = momento no vídeo onde o cutaway deve aparecer
6. "overlay_images": array com 8+ objetos {{"prompt": "descricao PT-BR", "insert_at": seconds, "duration": 2.5, "mode": "blur_overlay" ou "split"}}
   - 8+ imagens distribuidas pelo video, intercaladas com rosto do apresentador
   - Duration 2-3s cada
   - Timing NAO sobrepor com sora_videos (minimo 5s de distancia)
   - Alternar blur_overlay e split
   - Prompts: foto realista PT-BR, luz natural. NUNCA mencionar iPhone, camera ou dispositivo

{"Ignore hook_line1/hook_line2 do JSON, use estes:" + chr(10) + f"hook_line1: {hook_line1}" + chr(10) + f"hook_line2: {hook_line2}" if hook_line1 and hook_line2 else ""}

Transcrição:
{text}

Retorne APENAS o JSON, sem markdown."""

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    # Parse JSON (remover markdown se houver)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]

    return json.loads(content)


def generate_gemini_image(prompt, gemini_key, workdir, filename, max_retries=3):
    """Gera imagem com Gemini 3 Pro (com retry)."""
    import base64

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": f"Gere uma imagem fotorrealista, luz natural, sem filtro, alta resolucao, 16:9: {prompt}"}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                print(f"[REELS] Gemini attempt {attempt + 1}: no candidates. Response: {str(data)[:200]}", flush=True)
                time.sleep(3)
                continue

            for part in candidates[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    img_bytes = base64.b64decode(part["inlineData"]["data"])
                    path = os.path.join(workdir, filename)
                    with open(path, "wb") as f:
                        f.write(img_bytes)
                    return path

            print(f"[REELS] Gemini attempt {attempt + 1}: no image in response", flush=True)
            time.sleep(3)
        except Exception as e:
            print(f"[REELS] Gemini attempt {attempt + 1} error: {e}", flush=True)
            time.sleep(3)

    raise Exception("Gemini did not return an image")


def generate_sora_videos(sora_prompts, openai_key, workdir, target_w, target_h):
    """Gera vídeos Sora em paralelo, faz polling, download e resize."""
    headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}

    # Submit all jobs
    job_ids = []
    for i, spec in enumerate(sora_prompts):
        prompt = spec if isinstance(spec, str) else spec.get("prompt", "")
        resp = requests.post(
            "https://api.openai.com/v1/videos",
            headers=headers,
            json={"model": "sora-2", "prompt": prompt, "seconds": "4", "size": "720x1280"},
        )
        if resp.status_code == 200:
            vid_id = resp.json().get("id")
            insert_at = spec.get("insert_at", 15 + i * 20) if isinstance(spec, dict) else 15 + i * 20
            job_ids.append({"id": vid_id, "index": i, "insert_at": insert_at})
            print(f"[SORA] Job {i}: {vid_id}", flush=True)
        else:
            print(f"[SORA] Job {i} failed to submit: {resp.status_code}", flush=True)

    # Poll until all complete (max 5 min)
    deadline = time.time() + 300
    completed = set()
    paths = []

    while len(completed) < len(job_ids) and time.time() < deadline:
        time.sleep(15)
        for job in job_ids:
            if job["id"] in completed:
                continue
            resp = requests.get(
                f"https://api.openai.com/v1/videos/{job['id']}",
                headers={"Authorization": f"Bearer {openai_key}"},
            )
            status = resp.json().get("status")
            if status == "completed":
                completed.add(job["id"])
                # Download
                dl_resp = requests.get(
                    f"https://api.openai.com/v1/videos/{job['id']}/content",
                    headers={"Authorization": f"Bearer {openai_key}"},
                )
                raw_path = os.path.join(workdir, f"sora{job['index']}_raw.mp4")
                with open(raw_path, "wb") as f:
                    f.write(dl_resp.content)
                # Resize
                out_path = os.path.join(workdir, f"sora{job['index']}.mp4")
                subprocess.run([
                    "ffmpeg", "-y", "-i", raw_path,
                    "-vf", f"scale={target_w}:{target_h}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-an", out_path
                ], capture_output=True)
                paths.append({"path": out_path, "insert_at": job["insert_at"]})
                print(f"[SORA] Job {job['index']} completed", flush=True)
            elif status == "failed":
                completed.add(job["id"])
                print(f"[SORA] Job {job['index']} failed", flush=True)

    return paths


def filter_overlay_collisions(overlay_specs, sora_prompts):
    """Remove overlays que colidem com janelas de Sora cutaway (+0.5s buffer)."""
    sora_windows = []
    for spec in sora_prompts:
        t = spec.get("insert_at", 0) if isinstance(spec, dict) else 0
        sora_windows.append((t - 0.5, t + 4.5))  # 4s clip + 0.5s buffer each side

    filtered = []
    for ov in overlay_specs:
        ov_start = ov.get("insert_at", 0)
        ov_end = ov_start + ov.get("duration", 2.5)
        collides = False
        for sw_start, sw_end in sora_windows:
            if ov_start < sw_end and ov_end > sw_start:
                collides = True
                break
        if not collides:
            filtered.append(ov)
        else:
            print(f"[REELS] Overlay at {ov_start}s removed — collides with Sora cutaway", flush=True)
    return filtered


def build_timeline_map(segments, hook_dur):
    """Map original video timestamps to edited video timestamps.

    Edited video = hook (0..hook_dur) + segments concatenated.
    Segment 0 may start at hook_dur instead of its original start.
    Returns remap(orig_t) -> edited_t or None if outside included segments.
    """
    edited_pos = hook_dur
    intervals = []  # (orig_start, orig_end, edited_start)

    for i, seg in enumerate(segments):
        s = seg["start"] if isinstance(seg, dict) else seg[0]
        e = seg["end"] if isinstance(seg, dict) else seg[1]
        if i == 0 and s < hook_dur:
            s = hook_dur
        dur = e - s
        if dur <= 0:
            continue
        intervals.append((s, e, edited_pos))
        edited_pos += dur

    def remap(orig_t):
        for orig_s, orig_e, ed_s in intervals:
            if orig_s <= orig_t < orig_e:
                return ed_s + (orig_t - orig_s)
        return None

    return remap


def generate_overlay_images(overlay_specs, gemini_key, workdir):
    """Gera 8+ overlay images via Gemini. Pula falhas sem matar o pipeline."""
    results = []
    for i, spec in enumerate(overlay_specs):
        prompt = spec.get("prompt", "")
        if not prompt:
            continue
        try:
            path = generate_gemini_image(prompt, gemini_key, workdir, f"overlay_{i}.png")
            results.append({
                "path": path,
                "insert_at": spec.get("insert_at", 0),
                "duration": spec.get("duration", 2.5),
                "mode": spec.get("mode", "blur_overlay"),
            })
            print(f"[REELS] Overlay image {i} generated: {prompt[:50]}...", flush=True)
        except Exception as e:
            print(f"[REELS] Overlay image {i} failed (skipping): {e}", flush=True)
        time.sleep(2)  # rate limit Gemini
    return results


def build_hook_frames(W, H, hook_img_a_path, hook_img_b_path, line1, line2, video_path, workdir):
    """Constrói 2 hook frames: imagem topo + banner laranja programático + video embaixo."""
    img_h = int(W * 9 / 16)  # imagem 16:9 no topo

    # Banner proporcional ao tamanho do frame
    BANNER_W = int(W * 0.851)  # 919/1080
    BANNER_H = int(H * 0.115)  # 221/1920
    BANNER_CX = W // 2
    BANNER_CY = img_h  # centro do banner = divisão imagem/video
    BANNER_R = int(W * 0.025)  # 27/1080

    banner_x1 = BANNER_CX - BANNER_W // 2
    banner_y1 = BANNER_CY - BANNER_H // 2
    banner_x2 = banner_x1 + BANNER_W - 1
    banner_y2 = banner_y1 + BANNER_H - 1

    video_start_y = img_h
    CROP_TOP = H - (H - video_start_y)

    # Extrair frame do vídeo pra área inferior
    frame_path = os.path.join(workdir, "frame_face.png")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-vf", "fps=1", "-frames:v", "1", frame_path
    ], capture_output=True)
    face_frame = Image.open(frame_path).resize((W, H))

    # Auto-size: 2 fontes independentes (linha 2 maior para impacto)
    def find_font_size(text, max_w, target_h, min_size=20, max_size=120):
        for size in range(max_size, min_size, -1):
            font = ImageFont.truetype(FONT_PATH, size)
            bb = font.getbbox(text)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            if tw <= max_w and th <= target_h:
                return font, tw, th, bb[1]
        font = ImageFont.truetype(FONT_PATH, min_size)
        bb = font.getbbox(text)
        return font, bb[2] - bb[0], bb[3] - bb[1], bb[1]

    usable_w = BANNER_W - 40
    line1_target_h = int(BANNER_H * 0.33)
    line2_target_h = int(BANNER_H * 0.36)

    font1, tw1, th1, yo1 = find_font_size(line1, usable_w, line1_target_h)
    font2, tw2, th2, yo2 = find_font_size(line2, usable_w, line2_target_h)

    gap = int(BANNER_H * 0.045)
    total_th = th1 + gap + th2
    text_start_y = banner_y1 + (BANNER_H - total_th) // 2

    paths = []
    for idx, img_path in enumerate([hook_img_a_path, hook_img_b_path]):
        canvas = Image.new("RGB", (W, H), (0, 0, 0))

        # 1. Video from img_h downward
        canvas_arr = __import__('numpy').array(canvas)
        face_arr = __import__('numpy').array(face_frame)
        canvas_arr[img_h:H, :, :] = face_arr[img_h:H, :, :]
        canvas = Image.fromarray(canvas_arr)

        # 2. Hook decorative image from 0 to img_h
        hook_img = Image.open(img_path).resize((W, img_h))
        canvas.paste(hook_img, (0, 0))

        # 3. Programmatic banner on top (vibrant orange)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            [(banner_x1, banner_y1), (banner_x2, banner_y2)],
            radius=BANNER_R, fill=(255, 140, 0)
        )

        # 4. Text — 2 different sizes, centered
        draw.text((BANNER_CX - tw1 // 2, text_start_y - yo1), line1, fill="white", font=font1)
        draw.text((BANNER_CX - tw2 // 2, text_start_y + th1 + gap - yo2), line2, fill="white", font=font2)

        suffix = "a" if idx == 0 else "b"
        out_path = os.path.join(workdir, f"hook_frame_{suffix}.png")
        canvas.save(out_path)
        paths.append(out_path)

    return paths[0], paths[1], video_start_y, CROP_TOP


def edit_video(video_path, W, H, hook_frame_a_path, hook_frame_b_path,
               video_start_y, crop_top, segments, zoom_pattern, sora_specs, workdir):
    """Edita vídeo com ffmpeg puro: hook + zoom corte seco + cutaways. Baixo uso de RAM."""
    HOOK_DUR = 5.0
    IMG_SWITCH = 2.5
    video_area_h = H - video_start_y

    # --- 1. Criar hook clip (5s) com ffmpeg ---
    # Hook: imagem A (0-2.5s) + imagem B (2.5-5s) no topo, video cropado embaixo
    hook_a_clip = os.path.join(workdir, "hook_a.mp4")
    hook_b_clip = os.path.join(workdir, "hook_b.mp4")

    # Banner dimensions (must match build_hook_frames exactly)
    BANNER_H = int(H * 0.115)
    BANNER_Y = video_start_y - (BANNER_H // 2)

    for idx, (img_path, out_clip, dur) in enumerate([
        (hook_frame_a_path, hook_a_clip, IMG_SWITCH),
        (hook_frame_b_path, hook_b_clip, HOOK_DUR - IMG_SWITCH),
    ]):
        offset = 0 if idx == 0 else IMG_SWITCH
        # 3-layer composite: hook PNG (bg) + live video (middle) + banner crop (top)
        result = subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", img_path, "-t", str(dur),
            "-ss", str(offset), "-i", video_path, "-t", str(dur),
            "-filter_complex",
            f"[0:v]scale={W}:{H},split[bg][src];"
            f"[1:v]crop={W}:{video_area_h}:0:{crop_top}[vcrop];"
            f"[bg][vcrop]overlay=0:{video_start_y}[comp];"
            f"[src]crop={W}:{BANNER_H}:0:{BANNER_Y}[banner];"
            f"[comp][banner]overlay=0:{BANNER_Y}[out]",
            "-map", "[out]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k",
            "-r", "30", out_clip
        ], capture_output=True, text=True)
        if not os.path.exists(out_clip) or os.path.getsize(out_clip) < 100:
            print(f"[REELS] Hook clip {idx} FAILED. stderr: {result.stderr[-300:]}", flush=True)

    # Concatenar hook A + hook B
    hook_path = os.path.join(workdir, "hook.mp4")
    hook_list = os.path.join(workdir, "hook_list.txt")
    with open(hook_list, "w") as f:
        f.write(f"file '{hook_a_clip}'\nfile '{hook_b_clip}'\n")
    result = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", hook_list,
        "-c", "copy", hook_path
    ], capture_output=True, text=True)
    if not os.path.exists(hook_path) or os.path.getsize(hook_path) < 100:
        print(f"[REELS] Hook concat FAILED. stderr: {result.stderr[-300:]}", flush=True)

    # --- 2. Criar segmentos com zoom (ffmpeg) ---
    seg_paths = []
    # Obter duração do vídeo (resilient)
    video_duration = get_duration(video_path)
    print(f"[REELS] edit_video: video_duration={video_duration}s, segments={len(segments)}", flush=True)

    for i, seg in enumerate(segments):
        start = seg["start"] if isinstance(seg, dict) else seg[0]
        end = seg["end"] if isinstance(seg, dict) else seg[1]
        # First segment: skip content already used by hook (avoids audio/video repeating)
        if i == 0 and start < HOOK_DUR:
            start = HOOK_DUR
        end = min(end, video_duration)
        if start >= video_duration:
            break
        dur = end - start
        if dur <= 0:
            continue
        zoom = zoom_pattern[i % len(zoom_pattern)]
        seg_path = os.path.join(workdir, f"seg_{i}.mp4")

        if zoom == 1.0:
            # Sem zoom — corte direto
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", video_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k", "-r", "30", seg_path
            ], capture_output=True)
        else:
            # Zoom corte seco via ffmpeg: scale up + crop center
            zw = math.ceil(W * zoom)
            zh = math.ceil(H * zoom)
            zw += zw % 2
            zh += zh % 2
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", video_path,
                "-vf", f"scale={zw}:{zh},crop={W}:{H}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k", "-r", "30", seg_path
            ], capture_output=True)

        if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
            seg_paths.append(seg_path)
        else:
            print(f"[REELS] Segment {i} FAILED (start={start}, dur={dur}, zoom={zoom})", flush=True)

    # --- 3. Concatenar tudo: hook + segmentos ---
    concat_list = os.path.join(workdir, "concat_list.txt")
    with open(concat_list, "w") as f:
        f.write(f"file '{hook_path}'\n")
        for sp in seg_paths:
            f.write(f"file '{sp}'\n")

    out_path = os.path.join(workdir, "reels_noCaption.mp4")

    # Verify we have segments to concatenate
    if not seg_paths:
        print("[REELS] WARNING: No segments produced, using video directly", flush=True)
        # Fallback: just copy the video with re-encode
        result = subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k", "-r", "30",
            out_path
        ], capture_output=True, text=True)
        if not os.path.exists(out_path):
            print(f"[REELS] FFmpeg fallback stderr: {result.stderr[-500:]}", flush=True)
            raise RuntimeError("Failed to produce video file (no segments and fallback failed)")
    else:
        # Verify hook exists before concat
        if not os.path.exists(hook_path) or os.path.getsize(hook_path) < 1000:
            print("[REELS] Hook file missing/empty, concatenating segments only", flush=True)
            with open(concat_list, "w") as f:
                for sp in seg_paths:
                    f.write(f"file '{sp}'\n")

        result = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k", "-r", "30",
            out_path
        ], capture_output=True, text=True)

        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
            print(f"[REELS] Concat failed. stderr: {result.stderr[-500:]}", flush=True)
            print(f"[REELS] Concat list contents:", flush=True)
            with open(concat_list, "r") as f:
                print(f.read(), flush=True)
            # Last resort: just re-encode the original video
            print("[REELS] Attempting fallback: direct re-encode of source video", flush=True)
            result2 = subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "128k", "-r", "30",
                out_path
            ], capture_output=True, text=True)
            if not os.path.exists(out_path):
                raise RuntimeError(f"All video editing attempts failed. stderr: {result2.stderr[-300:]}")

    print(f"[REELS] Video concat done: {os.path.getsize(out_path)} bytes", flush=True)

    # --- 4. Sora cutaways (overlay) with Ken Burns ---
    if sora_specs:
        for si, spec in enumerate(sora_specs):
            try:
                fpath = spec["path"]
                insert_at = spec["insert_at"]
                if not os.path.exists(fpath):
                    continue
                temp_out = os.path.join(workdir, f"temp_sora_{si}.mp4")
                sora_dur = get_duration(fpath)
                if sora_dur <= 0:
                    continue
                # Ken Burns: slow push-in 1.0→1.06x over clip duration
                kb_total = int(sora_dur * 30)
                zoom_step = 0.06 / max(kb_total, 1)
                fade_out_st = max(sora_dur - 0.3, 0)
                subprocess.run([
                    "ffmpeg", "-y", "-i", out_path, "-i", fpath,
                    "-filter_complex",
                    f"[1:v]scale={W}:{H},"
                    f"zoompan=z='min(1+{zoom_step:.6f}*on,1.06)'"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                    f":d=1:s={W}x{H}:fps=30,"
                    f"fade=in:st=0:d=0.3:alpha=1,"
                    f"fade=out:st={fade_out_st:.2f}:d=0.3:alpha=1,"
                    f"setpts=PTS+{insert_at}/TB[ov];"
                    f"[0:v][ov]overlay=enable='between(t,{insert_at},{insert_at + sora_dur})'[out]",
                    "-map", "[out]", "-map", "0:a",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "copy", "-shortest", temp_out
                ], capture_output=True, text=True)
                if os.path.exists(temp_out) and os.path.getsize(temp_out) > 0:
                    os.replace(temp_out, out_path)
                    print(f"[REELS] Sora cutaway {si} applied with Ken Burns at {insert_at}s", flush=True)
                else:
                    print(f"[REELS] Sora cutaway {si} failed, skipping", flush=True)
            except Exception as e:
                print(f"[REELS] Sora cutaway {si} error: {e}, skipping", flush=True)

    print(f"[REELS] Video edited: {out_path}", flush=True)
    return out_path


def add_transition_effects(seg_path, W, H, workdir, seg_index):
    """Flash branco nos primeiros 0.15s do segmento. Single-pass re-encode (sem desync)."""
    final_path = os.path.join(workdir, f"seg_trans_{seg_index}.mp4")

    try:
        # Single pass: aplica fade from white nos primeiros 0.15s do segmento inteiro
        subprocess.run([
            "ffmpeg", "-y", "-i", seg_path,
            "-vf", f"fade=in:st=0:d=0.15:color=white",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k", "-r", "30",
            final_path
        ], capture_output=True, check=True)

        if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            print(f"[REELS] Transition effect applied to segment {seg_index}", flush=True)
            return final_path
    except Exception as e:
        print(f"[REELS] Transition effect failed for segment {seg_index}: {e}", flush=True)

    return seg_path  # fallback to original


def apply_image_overlays(video_path, overlay_data, W, H, workdir):
    """Aplica image overlays em batches de 3. Suporta blur_overlay e split modes."""
    if not overlay_data:
        return video_path

    current_path = video_path
    batch_size = 3

    for batch_idx in range(0, len(overlay_data), batch_size):
        batch = overlay_data[batch_idx:batch_idx + batch_size]
        temp_out = os.path.join(workdir, f"overlay_pass_{batch_idx}.mp4")

        # Build ffmpeg command with multiple inputs and filter graph
        inputs = ["-i", current_path]
        filter_parts = []
        overlay_chain = "[0:v]"

        for i, ov in enumerate(batch):
            img_path = ov["path"]
            t_start = ov["insert_at"]
            dur = ov["duration"]
            mode = ov.get("mode", "blur_overlay")
            t_end = t_start + dur
            inp_idx = i + 1

            inputs.extend(["-i", img_path])

            if mode == "split":
                # Split mode: image on top, video DISPLACED down (show top of frame, no stretch)
                img_h = int(W * 9 / 16)
                video_area_h = H - img_h
                filter_parts.append(
                    f"[{inp_idx}:v]scale={W}:{img_h},setsar=1[img{i}];"
                    f"{overlay_chain}split[base{i}][vsrc{i}];"
                    f"[vsrc{i}]crop={W}:{video_area_h}:0:0[vcrop{i}];"
                    f"[base{i}]drawbox=x=0:y=0:w={W}:h={H}:color=black:t=fill:"
                    f"enable='between(t,{t_start},{t_end})'[blk{i}];"
                    f"[blk{i}][vcrop{i}]overlay=0:{img_h}:"
                    f"enable='between(t,{t_start},{t_end})'[bot{i}];"
                    f"[bot{i}][img{i}]overlay=0:0:"
                    f"enable='between(t,{t_start},{t_end})'[ov{i}]"
                )
                overlay_chain = f"[ov{i}]"
            else:
                # blur_overlay mode: darkened bg + centered card (white border)
                img_w = int(W * 0.75)
                border = 6
                card_w = img_w + border * 2
                card_x = (W - card_w) // 2
                # Simplified filter: use drawbox for dark overlay instead of gblur+eq (more compatible)
                filter_parts.append(
                    f"[{inp_idx}:v]scale={img_w}:-1,setsar=1,"
                    f"pad={card_w}:ih+{border*2}:{border}:{border}:white[card{i}];"
                    f"{overlay_chain}drawbox=x=0:y=0:w={W}:h={H}:color=black@0.6:t=fill:"
                    f"enable='between(t,{t_start},{t_end})'[bg{i}];"
                    f"[bg{i}][card{i}]overlay={card_x}:(main_h-overlay_h)/2:"
                    f"enable='between(t,{t_start},{t_end})'[ov{i}]"
                )
                overlay_chain = f"[ov{i}]"

        full_filter = ";".join(filter_parts)
        # Final output label
        last_label = overlay_chain

        try:
            cmd = ["ffmpeg", "-y"] + inputs + [
                "-filter_complex", full_filter,
                "-map", last_label, "-map", "0:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "copy", temp_out
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if os.path.exists(temp_out) and os.path.getsize(temp_out) > 0:
                current_path = temp_out
                print(f"[REELS] Overlay batch {batch_idx//batch_size + 1} applied ({len(batch)} overlays)", flush=True)
            else:
                print(f"[REELS] Overlay batch {batch_idx//batch_size + 1} failed (empty output), stderr: {result.stderr[:300]}", flush=True)
        except Exception as e:
            print(f"[REELS] Overlay batch {batch_idx//batch_size + 1} error: {e}", flush=True)

    return current_path


def generate_sfx_pop(workdir):
    """Gera SFX soft pop programaticamente via ffmpeg (sine wave + fade)."""
    sfx_path = os.path.join(workdir, "sfx_pop.wav")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "sine=frequency=800:duration=0.08",
        "-af", "afade=in:st=0:d=0.02,afade=out:st=0.04:d=0.04",
        sfx_path
    ], capture_output=True, check=True)
    return sfx_path


def build_sfx_track(sfx_pop_path, timestamps, total_duration, workdir):
    """Constroi trilha SFX posicionando pops nos timestamps dados."""
    if not timestamps:
        return None

    sfx_track_path = os.path.join(workdir, "sfx_track.wav")

    # Build adelay filter chain: duplicate pop at each timestamp
    inputs = []
    filter_parts = []
    for i, ts in enumerate(timestamps):
        inputs.extend(["-i", sfx_pop_path])
        delay_ms = int(ts * 1000)
        filter_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[d{i}]")

    # Mix all delayed pops together
    mix_inputs = "".join(f"[d{i}]" for i in range(len(timestamps)))
    filter_parts.append(f"{mix_inputs}amix=inputs={len(timestamps)}:duration=longest:normalize=0[sfx]")

    # Pad/trim to total_duration
    filter_parts.append(f"[sfx]apad=whole_dur={total_duration}[out]")

    full_filter = ";".join(filter_parts)

    try:
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", full_filter,
            "-map", "[out]", "-t", str(total_duration),
            sfx_track_path
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(sfx_track_path) and os.path.getsize(sfx_track_path) > 0:
            print(f"[REELS] SFX track built with {len(timestamps)} pops", flush=True)
            return sfx_track_path
    except Exception as e:
        print(f"[REELS] SFX track build failed: {e}", flush=True)

    return None


def collect_sfx_timestamps(segments, overlay_data, hook_dur):
    """Coleta timestamps para SFX pops: transicoes de segmento + overlays."""
    timestamps = []

    # Transition timestamps (start of each segment except first)
    cumulative = hook_dur
    for i, seg in enumerate(segments):
        start = seg["start"] if isinstance(seg, dict) else seg[0]
        end = seg["end"] if isinstance(seg, dict) else seg[1]
        # Account for segment 0 adjustment (edit_video skips before hook_dur)
        if i == 0 and start < hook_dur:
            start = hook_dur
        dur = end - start
        if dur <= 0:
            continue
        if i > 0:
            timestamps.append(cumulative)
        cumulative += dur

    # Overlay timestamps (already remapped to edited timeline)
    for ov in overlay_data:
        timestamps.append(ov["insert_at"])

    timestamps.sort()
    return timestamps


def generate_captions(video_path, openai_key, W, H, workdir):
    """Gera captions ASS karaokê a partir do vídeo renderizado."""
    transcription = transcribe_whisper(video_path, openai_key, workdir)
    words = transcription.get("words", [])

    FONT_SIZE = max(int(60 * W / 1080), 20)
    WORDS_PER_LINE = 5

    lines = []
    for i in range(0, len(words), WORDS_PER_LINE):
        lines.append(words[i:i + WORDS_PER_LINE])

    def ts_to_ass(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    ass_content = f"""[Script Info]
Title: Karaoke Captions
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Helvetica Neue,{FONT_SIZE},&H0000B0FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,5,0,2,30,30,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for line_words in lines:
        start = line_words[0]["start"]
        end = line_words[-1]["end"]
        formatted = ""
        for j, w in enumerate(line_words):
            dur_cs = int((w["end"] - w["start"]) * 100)
            word_text = w["word"].strip()
            prefix = " " if j > 0 else ""
            formatted += f"{prefix}{{\\kf{dur_cs}}}{word_text}"
        events.append(f"Dialogue: 0,{ts_to_ass(start)},{ts_to_ass(end)},Karaoke,,0,0,0,,{formatted}")

    ass_content += "\n".join(events) + "\n"
    ass_path = os.path.join(workdir, "captions.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    return ass_path


def burn_captions_and_music(video_path, ass_path, workdir, sfx_track_path=None):
    """Burn ASS captions + 3-audio mix (voz + musica + SFX). Fallback graceful."""
    with_caption = os.path.join(workdir, "reels_withCaption.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"ass={ass_path}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        with_caption
    ], capture_output=True, check=True)

    final_path = os.path.join(workdir, "REELS_FINAL.mp4")

    has_music = os.path.exists(MUSIC_PATH)
    has_sfx = sfx_track_path and os.path.exists(sfx_track_path)

    if has_music and has_sfx:
        # 3-audio mix: voice + music + SFX
        print("[REELS] 3-audio mix: voice + music + SFX", flush=True)
        subprocess.run([
            "ffmpeg", "-y", "-i", with_caption, "-i", MUSIC_PATH, "-i", sfx_track_path,
            "-filter_complex",
            "[1:a]volume=0.04[m];[2:a]volume=0.20[sfx];[0:a][m][sfx]amix=inputs=3:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", final_path
        ], capture_output=True, check=True)
    elif has_music:
        # 2-audio mix: voice + music (original behavior)
        print("[REELS] 2-audio mix: voice + music", flush=True)
        subprocess.run([
            "ffmpeg", "-y", "-i", with_caption, "-i", MUSIC_PATH,
            "-filter_complex", "[1:a]volume=0.04[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", final_path
        ], capture_output=True, check=True)
    elif has_sfx:
        # 2-audio mix: voice + SFX
        print("[REELS] 2-audio mix: voice + SFX", flush=True)
        subprocess.run([
            "ffmpeg", "-y", "-i", with_caption, "-i", sfx_track_path,
            "-filter_complex", "[1:a]volume=0.20[sfx];[0:a][sfx]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", final_path
        ], capture_output=True, check=True)
    else:
        print("[REELS] No music/SFX found, copying audio as-is", flush=True)
        shutil.copy(with_caption, final_path)

    return final_path
