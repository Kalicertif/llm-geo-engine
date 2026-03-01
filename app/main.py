from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os
import time
import hmac
import hashlib
import json
import random
import requests
import openai

# =========================
# Configuration
# =========================

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL manquant")

# OpenAI config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2-chat-latest")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY manque")

openai.api_key = OPENAI_API_KEY

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
MAX_MEDIA = int(os.getenv("MAX_MEDIA", "20"))

# =========================
# Pydantic Models
# =========================

class GenerateIn(BaseModel):
    topic_key: str = Field(..., description="Topic pour lequel générer l’article")
    tone: str = Field(default="professionnel", description="Style tonalité")
    images_count: int = Field(default=2, ge=0, le=3, description="Nombre max images à insérer")

# =========================
# Helpers
# =========================

def db_connect():
    return psycopg.connect(DATABASE_URL)

def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    payload = f"{method}\n{path}\n{ts}\n{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def get_site(cur, site_id: str):
    cur.execute(
        "SELECT site_url, secret FROM sites WHERE id = %s AND is_active = true",
        (site_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Site non trouvé ou inactif")
    return row[0], row[1]

def memory_upsert(cur, site_id: str, key: str, value: dict):
    cur.execute(
        """
        INSERT INTO memories (site_id, key, value)
        VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (site_id, key)
        DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (site_id, key, json.dumps(value, ensure_ascii=False)),
    )

def memory_get(cur, site_id: str, key: str) -> dict:
    cur.execute("SELECT value FROM memories WHERE site_id = %s AND key = %s", (site_id, key))
    row = cur.fetchone()
    if not row:
        return {}
    val = row[0]
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except:
        return {}

def wp_signed_get(site_url: str, secret: str, call_path: str, sign_path: str) -> dict:
    ts = str(int(time.time()))
    sig = hmac_sign(secret, "GET", sign_path, ts, "")
    r = requests.get(
        site_url.rstrip("/") + call_path,
        headers={
            "X-LLMGEO-TS": ts,
            "X-LLMGEO-SIGN": sig,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"WP GET erreur {r.status_code}")
    return r.json()

def wp_signed_post(site_url: str, secret: str, path: str, body_json: str) -> dict:
    ts = str(int(time.time()))
    sig = hmac_sign(secret, "POST", path, ts, body_json)
    r = requests.post(
        site_url.rstrip("/") + path,
        data=body_json.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-LLMGEO-TS": ts,
            "X-LLMGEO-SIGN": sig,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"WP POST erreur {r.status_code}")
    return r.json()

def pick_images(media_cache: dict, k: int) -> list:
    return (media_cache.get("items") or [])[: max(0, min(k, len(media_cache.get("items") or [])))]

def build_internal_links_block(cur, site_id: str, topic_key: str, limit: int = 5):
    cur.execute(
        """
        SELECT title, wp_url
        FROM articles
        WHERE site_id = %s AND wp_url IS NOT NULL AND topic_key = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (site_id, topic_key, limit),
    )
    rows = cur.fetchall() or []
    if not rows:
        return ""
    block = "<h2>À lire aussi</h2><ul>"
    for t, url in rows:
        block += f'<li><a href="{url}">{t}</a></li>'
    block += "</ul>"
    return block

# =========================
# OpenAI Integration
# =========================

def openai_generate_article(prompt_text: str) -> dict:
    resp = openai.ChatCompletion.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":"You generate SEO-optimized articles in JSON only."},
            {"role":"user","content": prompt_text}
        ],
        max_tokens=2500,
    )
    text = resp.choices[0].message.content.strip()
    try:
        return json.loads(text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenAI output parse error: {str(e)}")

def build_openai_prompt(profile: dict, topic_key: str, style: str, lang: str) -> str:
    area = ", ".join(profile.get("business",{}).get("service_area",[]))
    company = profile.get("business",{}).get("company_name","")
    prompt = f"""
You are a professional SEO content writer.
Generate a full article in JSON with keys: title, excerpt, content_html.
Topic: {topic_key}
Style: {style}
Language: {lang}
Region: {area}
Company: {company}

Rules:
- Output valid JSON only.
- The content_html must include <h1>…</h1>, sections, and useful info.
- Include a FAQ section at the end.
- Optimize for local search relevance.

Begin.
"""
    return prompt.strip()

# =========================
# Endpoints
# =========================

@app.get("/health")
def health():
    return {"status":"ok"}

@app.post("/api/sites/{site_id}/analyze")
def analyze_site(site_id: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            site_url, secret = get_site(cur, site_id)

            profile = wp_signed_get(site_url, secret, "/wp-json/llmgeo/v1/site-profile", "/wp-json/llmgeo/v1/site-profile")
            media = wp_signed_get(site_url, secret, f"/wp-json/llmgeo/v1/media?per_page={MAX_MEDIA}", "/wp-json/llmgeo/v1/media")

            memory_upsert(cur, site_id, "site_profile", profile)
            memory_upsert(cur, site_id, "media_cache", media)

            # settings
            settings = profile.get("settings", {})
            memory_upsert(cur, site_id, "langs_enabled", settings.get("langs_enabled", ["fr"]))
            memory_upsert(cur, site_id, "styles_enabled", settings.get("styles_enabled", ["guide","tips","problem"]))
            memory_upsert(cur, site_id, "frequency", {"freq":settings.get("frequency","1_per_week")})

            conn.commit()
            return {"status":"ok"}

@app.get("/api/sites/{site_id}/topics")
def topics(site_id: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            profile = memory_get(cur, site_id, "site_profile")
            if not profile:
                raise HTTPException(status_code=400, detail="Site non analysé")

            # Simple deterministic topics logic (tu peux étendre)
            area = ", ".join((profile.get("business") or {}).get("service_area") or [])
            company = (profile.get("business") or {}).get("company_name","")
            base = []
            base.append({"topic_key":f"guide-{area}","title":f"Guide {area}","angle":"guide","site":company})
            return {"status":"ok","topics":base}

@app.post("/api/sites/{site_id}/generate-draft")
def generate_draft(site_id: str, payload: GenerateIn):
    with db_connect() as conn:
        with conn.cursor() as cur:
            profile = memory_get(cur, site_id, "site_profile")
            if not profile:
                raise HTTPException(status_code=400, detail="Site non analysé")

            langs = memory_get(cur, site_id, "langs_enabled") or ["fr"]
            styles = memory_get(cur, site_id, "styles_enabled") or ["guide","tips","problem"]

            # pick random lang & style
            lang = random.choice(langs)
            style = random.choice(styles)

            prompt = build_openai_prompt(profile, payload.topic_key, style, lang)
            out = openai_generate_article(prompt)

            title = out.get("title","")
            excerpt = out.get("excerpt","")
            content_html = out.get("content_html","")

            # duplicate check
            h = sha256_hex(content_html)
            cur.execute("SELECT wp_post_id FROM articles WHERE site_id=%s AND content_hash=%s",(site_id,h))
            dup = cur.fetchone()
            if dup:
                return {"status":"duplicate"}

            # internal links
            block = build_internal_links_block(cur, site_id, payload.topic_key)
            if block:
                content_html += "\n\n" + block

            body_json = json.dumps({"title":title,"content":content_html,"excerpt":excerpt}, ensure_ascii=False)
            site_url, secret = get_site(cur, site_id)
            wp_json = wp_signed_post(site_url, secret, "/wp-json/llmgeo/v1/draft", body_json)

            cur.execute(
                """
                INSERT INTO articles (
                    site_id, wp_post_id, wp_status, wp_url,
                    title, content_html, excerpt, topic_key, content_hash, meta
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    site_id,
                    wp_json.get("id"),
                    "draft",
                    wp_json.get("link"),
                    title,
                    content_html,
                    excerpt,
                    payload.topic_key,
                    h,
                    json.dumps({"lang":lang,"style":style}, ensure_ascii=False)
                ),
            )
            conn.commit()
            return {"status":"created","wp_url":wp_json.get("link")}
