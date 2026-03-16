"""
Supabase client — busca user_settings, faz upload pro Storage, persiste jobs.
"""

import os
import json
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
}


def get_user_settings(user_id: str) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/user_settings"
    params = {
        "user_id": f"eq.{user_id}",
        "select": "openai_api_key,gemini_api_key,instagram_username,instagram_full_name,instagram_profile_pic_url",
        "limit": "1",
    }
    resp = requests.get(url, headers=HEADERS, params=params)
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    return None


def upload_to_storage(bucket: str, path: str, file_path_or_bytes, content_type: str = "video/mp4") -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    headers = {**HEADERS, "Content-Type": content_type, "x-upsert": "true"}

    if isinstance(file_path_or_bytes, str):
        with open(file_path_or_bytes, "rb") as f:
            resp = requests.post(url, headers=headers, data=f)
    else:
        resp = requests.post(url, headers=headers, data=file_path_or_bytes)

    if resp.status_code in (200, 201):
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
        return public_url
    raise Exception(f"Storage upload failed ({resp.status_code}): {resp.text[:200]}")


def delete_from_storage(bucket: str, paths: list[str]) -> bool:
    """Deleta arquivos do Supabase Storage. Retorna True se sucesso."""
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"
    headers = {**HEADERS, "Content-Type": "application/json"}
    try:
        resp = requests.delete(url, headers=headers, json={"prefixes": paths})
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[SUPABASE] delete_from_storage error: {e}")
        return False


# ─── Jobs persistence (reels_jobs table) ───


def create_job(job_id: str, user_id: str, video_url: str = None) -> bool:
    """Cria job no Supabase. Retorna True se sucesso."""
    url = f"{SUPABASE_URL}/rest/v1/reels_jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"}
    payload = {
        "id": job_id,
        "user_id": user_id,
        "status": "processing",
        "progress": 0,
        "step": "starting",
        "video_url": video_url,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload)
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"[SUPABASE] create_job error: {e}")
        return False


def update_job(job_id: str, **fields) -> bool:
    """Atualiza campos do job no Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/reels_jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"}
    params = {"id": f"eq.{job_id}"}
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        resp = requests.patch(url, headers=headers, params=params, json=fields)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[SUPABASE] update_job error: {e}")
        return False


def get_job(job_id: str) -> dict | None:
    """Busca job por ID no Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/reels_jobs"
    params = {"id": f"eq.{job_id}", "select": "*", "limit": "1"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params)
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        print(f"[SUPABASE] get_job error: {e}")
    return None


def list_jobs(user_id: str = None, status: str = None, limit: int = 20) -> list:
    """Lista jobs. Filtra por user_id e/ou status."""
    url = f"{SUPABASE_URL}/rest/v1/reels_jobs"
    params = {
        "select": "id,user_id,status,progress,step,error,created_at,updated_at",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    if status:
        params["status"] = f"eq.{status}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params)
        data = resp.json()
        if isinstance(data, list):
            return data
    except Exception as e:
        print(f"[SUPABASE] list_jobs error: {e}")
    return []
