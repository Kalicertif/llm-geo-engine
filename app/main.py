from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
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
    # On veut le moteur multi-site => DB obligatoire
    # (sinon on ne peut pas récupérer les secrets HMAC par site)
    raise RuntimeError("DATABASE_URL manquant")


# =========================
# HELPERS
# =========================
def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """
    Doit matcher EXACTEMENT le plugin WP (même canon, mêmes sauts de ligne).
    Canon:
      METHOD \n PATH \n TS \n BODY
    """
    payload = f"{method}\n{path}\n{ts}\n{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


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
    status = "✅ WordPress configuré" if ok else "⚠️ WordPress non configuré (variables d'environnement manquantes)"
    return f"""
    <html>
      <head><title>Dashboard</title></head>
      <body style="font-family: sans-serif; margin: 2rem; max-width: 900px;">
        <h1>Dashboard</h1>
        <p>{status}</p>

        <h2>Test : créer un brouillon WordPress (mode legacy WP_* env)</h2>
        <form method="post" action="/wp/create-draft">
          <label>Titre</label><br/>
          <input name="title" style="width: 100%; padding: 8px" value="Test brouillon GEO"/><br/><br/>
          <label>Contenu (HTML)</label><br/>
          <textarea name="content" rows="10" style="width: 100%; padding: 8px">
<h2>Article test</h2>
<p>Ceci est un brouillon généré par le moteur.</p>
<h3>FAQ</h3>
<ul>
  <li><b>Q:</b> Exemple ? <b>R:</b> Oui.</li>
</ul>
          </textarea><br/><br/>
          <button type="submit" style="padding: 10px 14px;">Créer le brouillon</button>
        </form>

        <hr style="margin:2rem 0"/>

        <h2>Multi-site (nouveau) : /sites/{{site_id}}/draft</h2>
        <p>
          Ce endpoint utilise la base (tables <code>sites</code>, <code>articles</code>) et le plugin WP (HMAC).
          <br/>Il évite de recréer le même contenu via un hash.
        </p>
      </body>
    </html>
    """


# =========================
# LEGACY endpoint (WP_* env)
# =========================
@app.post("/wp/create-draft", response_class=HTMLResponse)
def create_draft(title: str = Form(...), content: str = Form(...)):
    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        return HTMLResponse("<p>❌ WP non configuré (WP_BASE_URL/WP_USERNAME/WP_APP_PASSWORD)</p>", status_code=400)

    # Timeout plus large + user-agent plus "browser friendly" dans le client WP
    wp = WordPressClient(WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD, timeout=60)

    try:
        post = wp.create_draft_post(title=title, content_html=content, excerpt="Brouillon test LLM GEO Engine")
    except ReadTimeout:
        # WordPress peut avoir créé le brouillon même si on n'a pas reçu la réponse.
        return HTMLResponse(
            """
            <html><body style="font-family:sans-serif;margin:2rem;">
              <h1>⚠️ Timeout côté WordPress</h1>
              <p>WordPress a mis trop de temps à répondre. Le brouillon a probablement été créé quand même.</p>
              <p>Vérifie dans WordPress → Articles → Brouillons.</p>
              <p><a href="/dashboard">Retour dashboard</a></p>
            </body></html>
            """,
            status_code=200,
        )
    except RequestException as e:
        return HTMLResponse(
            f"""
            <html><body style="font-family:sans-serif;margin:2rem;">
              <h1>❌ Erreur WordPress</h1>
              <p>Impossible de contacter l'API WordPress.</p>
              <pre style="white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:8px;">{str(e)}</pre>
              <p><a href="/dashboard">Retour dashboard</a></p>
            </body></html>
            """,
            status_code=200,
        )

    link = post.get("link", "")
    post_id = post.get("id", "")
    return f"""
    <html><body style="font-family:sans-serif;margin:2rem;">
      <h1>✅ Brouillon créé</h1>
      <p>ID: {post_id}</p>
      <p>Lien public (si WP le permet): <a href="{link}">{link}</a></p>
      <p>Va dans WordPress → Articles → Brouillons pour le voir.</p>
      <p><a href="/dashboard">Retour dashboard</a></p>
    </body></html>
    """


# =========================
# NEW multisite endpoint
# =========================
@app.post("/sites/{site_id}/draft")
def create_multisite_draft(
    site_id: str,
    title: str = Form(...),
    content: str = Form(...),
    excerpt: str = Form(""),
    topic_key: str = Form("general"),
):
    """
    Crée un brouillon sur le site WordPress identifié par site_id.
    - Récupère site_url + secret HMAC dans la table sites
    - Anti-duplicate par hash du content
    - Appelle le plugin WP: POST /wp-json/llmgeo/v1/draft avec headers HMAC
    - Enregistre l'article en base
    """

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # 1) site
            cur.execute(
                "SELECT site_url, secret FROM sites WHERE id = %s AND is_active = true",
                (site_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Site non trouvé ou inactif")

            site_url, secret = row

            # 2) anti-duplicate
            content_hash = sha256_hex(content)
            cur.execute(
                "SELECT wp_post_id, wp_url FROM articles WHERE site_id = %s AND content_hash = %s",
                (site_id, content_hash),
            )
            dup = cur.fetchone()
            if dup:
                return {"status": "duplicate", "wp_post_id": dup[0], "wp_url": dup[1]}

            # 3) call WP plugin
            wp_path = "/wp-json/llmgeo/v1/draft"
            wp_url = site_url.rstrip("/") + wp_path
            ts = str(int(time.time()))

            # IMPORTANT: signer EXACTEMENT le body envoyé (json string)
            body = json.dumps(
                {"title": title, "content": content, "excerpt": excerpt},
                ensure_ascii=False,
            )
            sign = hmac_sign(secret, "POST", wp_path, ts, body)

            try:
                r = requests.post(
                    wp_url,
                    data=body.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-LLMGEO-TS": ts,
                        "X-LLMGEO-SIGN": sign,
                    },
                    timeout=60,
                )
            except RequestException as e:
                raise HTTPException(status_code=502, detail=f"Erreur requête WP: {str(e)}")

            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Erreur WP ({r.status_code}): {r.text}")

            data = r.json()
            wp_post_id = data.get("id")
            wp_link = data.get("link")

            # 4) store in DB
            cur.execute(
                """
                INSERT INTO articles (
                    site_id, wp_post_id, wp_status, wp_url,
                    title, content_html, excerpt,
                    topic_key, content_hash, meta
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)
                """,
                (
                    site_id,
                    wp_post_id,
                    "draft",
                    wp_link,
                    title,
                    content,
                    excerpt,
                    topic_key,
                    content_hash,
                ),
            )
            conn.commit()

            return {"status": "created", "wp_post_id": wp_post_id, "wp_url": wp_link}
