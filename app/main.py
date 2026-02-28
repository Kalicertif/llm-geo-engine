from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
import time
import hmac
import hashlib
import json

import psycopg
import requests
from requests.exceptions import ReadTimeout, RequestException

from app.wp import WordPressClient

app = FastAPI()

# =========================
# ENV / CONFIG
# =========================
WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip()
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL manquant")


# =========================
# HELPERS
# =========================
def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    payload = f"{method}\n{path}\n{ts}\n{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# =========================
# Pydantic Model (JSON API)
# =========================
class DraftIn(BaseModel):
    title: str
    content_html: str
    excerpt: str = ""
    topic_key: str = "general"


# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return {"status": "ok", "environment": os.getenv("ENVIRONMENT", "unknown")}


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
      <head><title>LLM GEO Engine</title></head>
      <body style="font-family: sans-serif; margin: 2rem;">
        <h1>LLM GEO Engine</h1>
        <p>✅ API en ligne.</p>
        <ul>
          <li><a href="/health">/health</a></li>
          <li><a href="/dashboard">/dashboard</a></li>
        </ul>
      </body>
    </html>
    """


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    ok = bool(WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD)
    status = "✅ WordPress configuré" if ok else "⚠️ WordPress non configuré"

    return f"""
    <html>
      <body style="font-family: sans-serif; margin: 2rem;">
        <h1>Dashboard</h1>
        <p>{status}</p>

        <h2>Legacy WP (Application Password)</h2>
        <form method="post" action="/wp/create-draft">
          <input name="title" placeholder="Titre"/><br/><br/>
          <textarea name="content" rows="6" placeholder="Contenu"></textarea><br/><br/>
          <button type="submit">Créer brouillon</button>
        </form>
      </body>
    </html>
    """


# =========================
# LEGACY WP ENDPOINT
# =========================
@app.post("/wp/create-draft", response_class=HTMLResponse)
def create_draft(title: str = Form(...), content: str = Form(...)):
    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        return HTMLResponse("WP non configuré", status_code=400)

    wp = WordPressClient(WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD, timeout=60)

    try:
        post = wp.create_draft_post(title=title, content_html=content, excerpt="Test GEO Engine")
    except ReadTimeout:
        return HTMLResponse("Timeout WP (brouillon probablement créé)")
    except RequestException as e:
        return HTMLResponse(f"Erreur WP: {str(e)}")

    return HTMLResponse(f"Brouillon créé ID: {post.get('id')}")


# =========================
# CORE MULTI-SITE LOGIC
# =========================
def create_multisite_draft_internal(site_id: str, title: str, content: str, excerpt: str, topic_key: str):
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:

            # 1️⃣ récupérer site
            cur.execute(
                "SELECT site_url, secret FROM sites WHERE id = %s AND is_active = true",
                (site_id,)
            )
            site = cur.fetchone()

            if not site:
                raise HTTPException(status_code=404, detail="Site non trouvé")

            site_url, secret = site

            # 2️⃣ anti-duplicate (hash sur contenu de base, AVANT maillage)
            base_hash = sha256_hex(content)

            cur.execute(
                "SELECT wp_post_id, wp_url FROM articles WHERE site_id = %s AND content_hash = %s",
                (site_id, base_hash),
            )
            duplicate = cur.fetchone()

            if duplicate:
                return {
                    "status": "duplicate",
                    "wp_post_id": duplicate[0],
                    "wp_url": duplicate[1],
                }

            # 3️⃣ maillage interne simple (5 derniers)
            cur.execute(
                """
                SELECT title, wp_url
                FROM articles
                WHERE site_id = %s AND wp_url IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (site_id,),
            )
            related_articles = cur.fetchall()

            if related_articles:
                internal_block = "<h2>À lire aussi</h2><ul>"
                for t, url in related_articles:
                    internal_block += f'<li><a href="{url}">{t}</a></li>'
                internal_block += "</ul>"
                content = content + "\n\n" + internal_block

            # 4️⃣ appel plugin WP (HMAC)
            wp_path = "/wp-json/llmgeo/v1/draft"
            wp_url = site_url.rstrip("/") + wp_path
            ts = str(int(time.time()))

            body_json = json.dumps(
                {"title": title, "content": content, "excerpt": excerpt},
                ensure_ascii=False,
            )

            signature = hmac_sign(secret, "POST", wp_path, ts, body_json)

            response = requests.post(
                wp_url,
                data=body_json.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-LLMGEO-TS": ts,
                    "X-LLMGEO-SIGN": signature,
                },
                timeout=60,
            )

            if response.status_code >= 400:
                raise HTTPException(status_code=502, detail=response.text)

            wp_json = response.json()

            # 5️⃣ enregistrer en base
            cur.execute(
                """
                INSERT INTO articles (
                    site_id,
                    wp_post_id,
                    wp_status,
                    wp_url,
                    title,
                    content_html,
                    excerpt,
                    topic_key,
                    content_hash,
                    meta
                )
                VALUES (%s,%s,'draft',%s,%s,%s,%s,%s,%s,'{}'::jsonb)
                """,
                (
                    site_id,
                    wp_json.get("id"),
                    wp_json.get("link"),
                    title,
                    content,
                    excerpt,
                    topic_key,
                    base_hash,
                ),
            )

            conn.commit()

            return {
                "status": "created",
                "wp_post_id": wp_json.get("id"),
                "wp_url": wp_json.get("link"),
            }


# =========================
# MULTI-SITE (FORM)
# =========================
@app.post("/sites/{site_id}/draft")
def create_multisite_draft(
    site_id: str,
    title: str = Form(...),
    content: str = Form(...),
    excerpt: str = Form(""),
    topic_key: str = Form("general"),
):
    return create_multisite_draft_internal(site_id, title, content, excerpt, topic_key)


# =========================
# MULTI-SITE (JSON - RECOMMANDÉ)
# =========================
@app.post("/api/sites/{site_id}/draft")
def create_multisite_draft_json(site_id: str, payload: DraftIn):
    return create_multisite_draft_internal(
        site_id=site_id,
        title=payload.title,
        content=payload.content_html,
        excerpt=payload.excerpt,
        topic_key=payload.topic_key,
    )
