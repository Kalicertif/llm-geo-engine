from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
import os
import time
import hmac
import hashlib
import json
import random
import html
import re
import requests
import psycopg

from openai import OpenAI

app = FastAPI()

# =====================
# Env
# =====================

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL manquant")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")  # modèle par défaut safe
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY manquant")

client = OpenAI(api_key=OPENAI_API_KEY)

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
MAX_MEDIA = int(os.getenv("MAX_MEDIA", "20"))

# Optionnel : sécuriser les endpoints d’écriture
ENGINE_ADMIN_TOKEN = os.getenv("ENGINE_ADMIN_TOKEN", "").strip()


# =====================
# Models
# =====================

class GenerateIn(BaseModel):
    topic_key: str = Field(..., description="Cluster / sujet à traiter")
    frequency: str = Field(default="1_per_week", description="Fréquence retenue pour planification SEO")
    images_count: int = Field(default=2, ge=0, le=3)


# =====================
# Helpers DB
# =====================

def db_connect():
    return psycopg.connect(DATABASE_URL)


def require_admin_token(x_engine_token: str | None):
    if not ENGINE_ADMIN_TOKEN:
        return
    if not x_engine_token or x_engine_token != ENGINE_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# =====================
# Helpers HMAC / WP
# =====================

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
        (site_id, key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
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
    except Exception:
        return {}


def wp_signed_get(site_url: str, secret: str, call_path: str, sign_path: str) -> dict:
    ts = str(int(time.time()))
    sig = hmac_sign(secret, "GET", sign_path, ts, "")
    r = requests.get(
        site_url.rstrip("/") + call_path,
        headers={"X-LLMGEO-TS": ts, "X-LLMGEO-SIGN": sig},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"WP GET erreur {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="WP GET réponse non-JSON")


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
        raise HTTPException(status_code=502, detail=f"WP POST erreur {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="WP POST réponse non-JSON")


# =====================
# Content helpers
# =====================

def pick_images(media_cache: dict, k: int) -> list[dict]:
    items = (media_cache.get("items") or [])
    if k <= 0 or not items:
        return []
    if len(items) <= k:
        return items
    return random.sample(items, k)


def image_to_figure_html(img: dict) -> str:
    url = img.get("url") or ""
    alt = (img.get("alt") or "").strip()
    caption = (img.get("caption") or "").strip()
    width = img.get("width")
    height = img.get("height")

    if not url:
        return ""

    alt_esc = html.escape(alt, quote=True) if alt else ""
    cap_esc = html.escape(caption, quote=False) if caption else ""
    w_attr = f' width="{int(width)}"' if isinstance(width, int) else ""
    h_attr = f' height="{int(height)}"' if isinstance(height, int) else ""

    fig = f'<figure class="llmgeo-media"><img src="{html.escape(url, quote=True)}" alt="{alt_esc}" loading="lazy"{w_attr}{h_attr}>'
    if cap_esc:
        fig += f"<figcaption>{cap_esc}</figcaption>"
    fig += "</figure>"
    return fig


def inject_figures_into_html(content_html: str, figures: list[str]) -> str:
    figures = [f for f in figures if f]
    if not figures:
        return content_html

    html_in = content_html or ""
    # inject after first paragraph if possible
    m = re.search(r"</p\s*>", html_in, flags=re.IGNORECASE)
    if m:
        idx = m.end()
        first = figures[0]
        rest = figures[1:]
        html_in = html_in[:idx] + "\n" + first + "\n" + html_in[idx:]
        if rest:
            # inject remaining near middle
            parts = html_in.split("</h2>")
            if len(parts) >= 2:
                mid = len(parts) // 2
                parts[mid] = parts[mid] + "\n" + "\n".join(rest) + "\n"
                html_in = "</h2>".join(parts)
            else:
                html_in += "\n" + "\n".join(rest) + "\n"
        return html_in

    # fallback: prepend
    return "\n".join(figures) + "\n" + html_in


def build_internal_links_block(cur, site_id: str, topic_key: str, limit: int = 5) -> str:
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
        if not url:
            continue
        t_safe = html.escape(t or "", quote=False)
        url_safe = html.escape(url, quote=True)
        block += f'<li><a href="{url_safe}">{t_safe}</a></li>'
    block += "</ul>"
    return block


# =====================
# OpenAI generation
# =====================

def extract_json_object(text: str) -> dict:
    """
    Best-effort extraction if the model outputs junk around JSON.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try find first {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        chunk = text[start : end + 1]
        try:
            return json.loads(chunk)
        except Exception:
            return {}
    return {}


def openai_generate_article(prompt_text: str) -> dict:
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Return STRICT JSON only. No markdown. No extra text."},
                {"role": "user", "content": prompt_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenAI error: {str(e)}")

    text = (resp.choices[0].message.content or "").strip()
    out = extract_json_object(text)
    if not out:
        raise HTTPException(status_code=500, detail="OpenAI output JSON invalide")
    return out


def build_openai_prompt(profile: dict, topic_key: str, frequency: str, lang: str) -> str:
    site = profile.get("site", {}) or {}
    biz = profile.get("business", {}) or {}
    settings = profile.get("settings", {}) or {}

    region = ", ".join(biz.get("service_area") or [])
    company = biz.get("company_name", "")
    target = biz.get("target_audience", "")
    services = biz.get("primary_services") or []
    tone = settings.get("tone") or "professional"
    geo_focus = settings.get("geo_focus") or region

    prompt = f"""
You are a professional SEO writer. Generate a full article in JSON.

OUTPUT (strict JSON) with keys:
- title (string)
- excerpt (string)
- content_html (string)  // HTML with <h1>, <h2>, paragraphs, lists
- meta (object) with keys: meta_title, meta_description, primary_keyword, faq (array)

SITE PROFILE:
Company: {company}
Site name: {site.get("name","")}
Main language: {lang}
Target audience: {target}
Services: {services}
Geo focus (must be explicit in text): {geo_focus}

TOPIC:
topic_key: {topic_key}
frequency: {frequency}

REQUIREMENTS:
- content_html must include: an intro, at least 3 <h2> sections, a conclusion.
- Include a local angle: mention the service area / city/region naturally.
- Include an FAQ section with at least 3 Q&A (also in meta.faq).
- Write for humans first, but keep strong SEO structure.
- Do NOT include images or internal links placeholders. Engine injects them.

Return STRICT JSON only.
"""
    return prompt.strip()


# =====================
# Endpoints
# =====================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/sites/{site_id}/analyze")
def analyze_site(site_id: str, x_engine_token: str | None = Header(default=None)):
    require_admin_token(x_engine_token)

    with db_connect() as conn:
        with conn.cursor() as cur:
            site_url, secret = get_site(cur, site_id)

            profile = wp_signed_get(
                site_url,
                secret,
                "/wp-json/llmgeo/v1/site-profile",
                "/wp-json/llmgeo/v1/site-profile",
            )
            media = wp_signed_get(
                site_url,
                secret,
                f"/wp-json/llmgeo/v1/media?per_page={MAX_MEDIA}",
                "/wp-json/llmgeo/v1/media",
            )

            memory_upsert(cur, site_id, "site_profile", profile)
            memory_upsert(cur, site_id, "media_cache", media)
            conn.commit()

    return {"status": "ok"}


@app.post("/api/sites/{site_id}/generate-draft")
def generate_draft(site_id: str, payload: GenerateIn, x_engine_token: str | None = Header(default=None)):
    require_admin_token(x_engine_token)

    with db_connect() as conn:
        with conn.cursor() as cur:
            profile = memory_get(cur, site_id, "site_profile")
            media = memory_get(cur, site_id, "media_cache")
            if not profile:
                raise HTTPException(status_code=400, detail="Site non analysé")

            # Language selection: prefer plugin settings if present
            langs = (profile.get("settings", {}) or {}).get("langs_enabled") or []
            if not langs:
                langs = [(profile.get("site", {}) or {}).get("language", "fr_FR").split("_")[0]]
            lang = random.choice(langs)

            prompt = build_openai_prompt(profile, payload.topic_key, payload.frequency, lang)
            out = openai_generate_article(prompt)

            title = (out.get("title") or "").strip()
            excerpt = (out.get("excerpt") or "").strip()
            content_html = (out.get("content_html") or "").strip()

            if not title or not content_html:
                raise HTTPException(status_code=500, detail="OpenAI output incomplet (title/content_html)")

            # Duplicate check
            h = sha256_hex(content_html)
            cur.execute(
                "SELECT wp_post_id FROM articles WHERE site_id=%s AND content_hash=%s",
                (site_id, h),
            )
            if cur.fetchone():
                return {"status": "duplicate"}

            # Internal links (same topic)
            internal_block = build_internal_links_block(cur, site_id, payload.topic_key)
            if internal_block:
                content_html += "\n\n" + internal_block

            # Inject images from WP media
            imgs = pick_images(media or {}, payload.images_count)
            figures = [image_to_figure_html(i) for i in imgs]
            content_html = inject_figures_into_html(content_html, figures)

            # Create draft on WP via HMAC plugin
            site_url, secret = get_site(cur, site_id)
            wp_payload = {"title": title, "content": content_html, "excerpt": excerpt}
            body = json.dumps(wp_payload, ensure_ascii=False, separators=(",", ":"))
            wp_resp = wp_signed_post(site_url, secret, "/wp-json/llmgeo/v1/draft", body)

            cur.execute(
                """
                INSERT INTO articles (
                    site_id, wp_post_id, wp_status, wp_url,
                    title, content_html, excerpt, topic_key, content_hash, meta
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    site_id,
                    wp_resp.get("id"),
                    "draft",
                    wp_resp.get("link"),
                    title,
                    content_html,
                    excerpt,
                    payload.topic_key,
                    h,
                    json.dumps(
                        {
                            "frequency": payload.frequency,
                            "lang": lang,
                            "notified": wp_resp.get("notified"),
                            "edit_link": wp_resp.get("edit_link"),
                            "openai_meta": out.get("meta") or {},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            conn.commit()

            return {
                "status": "created",
                "wp_url": wp_resp.get("link"),
                "edit_link": wp_resp.get("edit_link"),
                "notified": wp_resp.get("notified"),
            }
