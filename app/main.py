from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import os
import time
import hmac
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

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

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
MAX_MEDIA = int(os.getenv("MAX_MEDIA", "20"))  # nb d'images à récupérer

# =========================
# HELPERS
# =========================
def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """
    Plugin WP signe: METHOD\nPATH\nTS\nBODY
    Où PATH est le path canonique SANS host, généralement sans query (selon ton hmac.php).
    """
    payload = f"{method}\n{path}\n{ts}\n{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def db_connect():
    return psycopg.connect(DATABASE_URL)

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

def memory_get(cur, site_id: str, key: str) -> Optional[dict]:
    cur.execute("SELECT value FROM memories WHERE site_id = %s AND key = %s", (site_id, key))
    row = cur.fetchone()
    if not row:
        return None
    # psycopg peut retourner dict directement (jsonb), sinon string
    val = row[0]
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        return json.loads(val)
    return val

def get_site(cur, site_id: str) -> Tuple[str, str]:
    cur.execute(
        "SELECT site_url, secret FROM sites WHERE id = %s AND is_active = true",
        (site_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Site non trouvé (ou inactif)")
    site_url, secret = row
    if not site_url or not secret:
        raise HTTPException(status_code=400, detail="Site mal configuré (site_url/secret manquant)")
    return site_url, secret

def wp_signed_get(site_url: str, secret: str, call_path: str, sign_path: str) -> dict:
    """
    call_path: path réellement appelé côté WP (peut inclure ?query)
    sign_path: path signé (selon ton plugin, canonical_path = PATH sans query)
    """
    ts = str(int(time.time()))
    body = ""  # GET => body vide
    sig = hmac_sign(secret, "GET", sign_path, ts, body)

    url = site_url.rstrip("/") + call_path
    r = requests.get(
        url,
        headers={
            "X-LLMGEO-TS": ts,
            "X-LLMGEO-SIGN": sig,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"WP GET {call_path} failed: {r.status_code} {r.text}")
    return r.json()

def wp_signed_post(site_url: str, secret: str, path: str, body_json: str) -> dict:
    ts = str(int(time.time()))
    sig = hmac_sign(secret, "POST", path, ts, body_json)

    url = site_url.rstrip("/") + path
    r = requests.post(
        url,
        data=body_json.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-LLMGEO-TS": ts,
            "X-LLMGEO-SIGN": sig,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"WP POST {path} failed: {r.status_code} {r.text}")
    return r.json()

def pick_images(media_cache: dict, k: int = 2) -> List[dict]:
    items = (media_cache or {}).get("items") or []
    # On prend les plus récentes (déjà triées côté WP), on garde k
    return items[: max(0, min(k, len(items)))]

def normalize_service_area(profile: dict) -> str:
    areas = (((profile or {}).get("business") or {}).get("service_area") or [])
    if isinstance(areas, list) and areas:
        # e.g. "La Réunion, Saint-Pierre, Saint-Leu"
        return ", ".join([str(x).strip() for x in areas if str(x).strip()])
    # fallback
    return "La Réunion"

def get_company_name(profile: dict) -> str:
    b = (profile or {}).get("business") or {}
    return (b.get("company_name") or (profile.get("site", {}) if profile else {}).get("site_name") or "Notre entreprise").strip()

def build_faq_jsonld(faq: List[Tuple[str, str]]) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            }
            for (q, a) in faq
        ],
    }
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"

def build_article_html(profile: dict, media_cache: dict, topic_key: str, angle: str) -> Tuple[str, str, str, List[str]]:
    """
    Retourne: (title, content_html, excerpt, images_used_urls)
    Article "vrai" mais déterministe (sans LLM), pour valider le pipeline.
    Ensuite tu pourras brancher un LLM sur cette structure.
    """
    company = get_company_name(profile)
    area = normalize_service_area(profile)
    site_name = ((profile or {}).get("site") or {}).get("site_name") or company

    # Heuristique simple: toiture (vu media + alt)
    # Tu pourras améliorer via "business.category" et "primary_services"
    title = f"Entretien et nettoyage de toiture à {area} : guide complet (mousses, hydrofuge, peinture)"
    excerpt = f"Conseils pratiques pour prolonger la durée de vie de votre toiture à {area} : nettoyage, démoussage, hydrofuge et peinture, avec points de vigilance."

    imgs = pick_images(media_cache, k=2)
    images_used = [i.get("url") for i in imgs if i.get("url")]

    # Bloc images (simple, SEO friendly)
    img_block = ""
    if images_used:
        img_block += "<h2>Exemples de chantiers</h2>"
        for i in imgs:
            url = i.get("url")
            alt = (i.get("alt") or i.get("title") or "Illustration").strip()
            if url:
                img_block += f"""
<figure>
  <img src="{url}" alt="{alt}" style="max-width:100%;height:auto;border-radius:12px" loading="lazy"/>
  <figcaption style="font-size:0.95em;opacity:0.85">{alt}</figcaption>
</figure>
"""

    # FAQ
    faq = [
        (f"À quelle fréquence nettoyer sa toiture à {area} ?", "En climat humide, un contrôle annuel est recommandé, avec un nettoyage dès l’apparition de mousses/lichens."),
        ("Le démoussage abîme-t-il les tuiles ?", "Si la méthode est adaptée (pression maîtrisée, produits compatibles), le démoussage protège plutôt qu’il n’abîme."),
        ("Hydrofuge : utile ou pas ?", "Oui, si la toiture est saine : l’hydrofuge limite la pénétration d’eau et ralentit le retour des mousses."),
        ("Peut-on peindre une toiture ?", "Oui, après préparation (nettoyage, réparations, primaire si besoin) et avec une peinture adaptée au support."),
        ("Quel budget prévoir ?", "Le prix dépend de la surface, de l’accessibilité, de l’état, et du traitement choisi (nettoyage seul vs hydrofuge/peinture)."),
    ]
    faq_html = "<h2>FAQ</h2><ul>"
    for q, a in faq:
        faq_html += f"<li><b>{q}</b><br/>{a}</li>"
    faq_html += "</ul>"
    faq_jsonld = build_faq_jsonld(faq)

    # Contenu GEO/SEO
    content = f"""
<h1>{title}</h1>

<p><i>Dernière mise à jour : {time.strftime("%d/%m/%Y")}</i></p>

<p>À <b>{area}</b>, l’humidité, la chaleur et la végétation favorisent l’apparition de <b>mousses</b> et <b>lichens</b> sur les toitures.
Un entretien régulier aide à éviter les infiltrations, à préserver l’esthétique et à prolonger la durée de vie de la couverture.</p>

<h2>Pourquoi entretenir sa toiture à {area} ?</h2>
<ul>
  <li><b>Limiter les infiltrations</b> : les mousses retiennent l’eau et fragilisent certains matériaux.</li>
  <li><b>Éviter les dégradations</b> : tuiles poreuses, joints affaiblis, gouttières encombrées.</li>
  <li><b>Valoriser le bien</b> : une toiture propre améliore l’apparence et rassure lors d’une vente/location.</li>
</ul>

<h2>Les principales méthodes : nettoyage, démoussage, hydrofuge, peinture</h2>

<h3>1) Nettoyage / décrassage</h3>
<p>Le nettoyage retire la saleté, les poussières et une partie des micro-organismes.
La clé est d’utiliser une méthode <b>adaptée au matériau</b> (tuiles, tôles, etc.) et à l’état général.</p>

<h3>2) Démoussage</h3>
<p>Le démoussage vise à éliminer mousses et lichens. Il peut combiner action mécanique et traitement.
Après le traitement, il faut souvent laisser agir puis rincer selon les recommandations du produit.</p>

<h3>3) Traitement hydrofuge</h3>
<p>Un hydrofuge améliore la résistance à l’eau et ralentit la réapparition des mousses.
Il s’applique sur une toiture <b>propre et saine</b>. On évite sur supports non compatibles ou trop dégradés.</p>

<h3>4) Peinture toiture</h3>
<p>La peinture peut protéger et uniformiser l’aspect. Elle demande une préparation sérieuse (nettoyage, réparations, primaire si nécessaire).</p>

<h2>Erreurs fréquentes à éviter</h2>
<ul>
  <li>Utiliser une pression trop forte sur un support fragile.</li>
  <li>Appliquer un produit sans vérifier la compatibilité avec le matériau.</li>
  <li>Oublier les gouttières et les points singuliers (rives, faîtage, solins).</li>
  <li>Intervenir sans sécurité (harnais, stabilité, météo).</li>
</ul>

<h2>Conseils pratiques pour un entretien durable</h2>
<ul>
  <li>Inspecter la toiture après la saison cyclonique / fortes pluies.</li>
  <li>Surveiller les zones ombragées (retour mousse plus rapide).</li>
  <li>Prévoir un nettoyage préventif avant que la mousse ne s’installe fortement.</li>
</ul>

{img_block}

{faq_html}
{faq_jsonld}

<hr/>
<p><b>{company}</b> — {site_name}. Zone d’intervention : <b>{area}</b>.</p>
"""

    return title, content.strip(), excerpt, images_used

def build_internal_links_block(cur, site_id: str, topic_key: str, limit: int = 5) -> str:
    cur.execute(
        """
        SELECT title, wp_url
        FROM articles
        WHERE site_id = %s
          AND wp_url IS NOT NULL
          AND topic_key = %s
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
        if url:
            block += f'<li><a href="{url}">{t}</a></li>'
    block += "</ul>"
    return block

# =========================
# Pydantic Models
# =========================
class DraftIn(BaseModel):
    title: str
    content_html: str
    excerpt: str = ""
    topic_key: str = "general"

class GenerateIn(BaseModel):
    topic_key: str = Field(default="toiture-entretien", description="Cluster/topic_key pour maillage")
    angle: str = Field(default="guide", description="Angle éditorial (guide, tips, problem...)")
    images_count: int = Field(default=2, ge=0, le=3)

# =========================
# ROUTES (basic)
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
# LEGACY WP ENDPOINT (optional)
# =========================
@app.post("/wp/create-draft", response_class=HTMLResponse)
def create_draft(title: str = Form(...), content: str = Form(...)):
    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        return HTMLResponse("WP non configuré", status_code=400)

    wp = WordPressClient(WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD, timeout=HTTP_TIMEOUT)

    try:
        post = wp.create_draft_post(title=title, content_html=content, excerpt="Test GEO Engine")
    except ReadTimeout:
        return HTMLResponse("Timeout WP (brouillon probablement créé)")
    except RequestException as e:
        return HTMLResponse(f"Erreur WP: {str(e)}")

    return HTMLResponse(f"Brouillon créé ID: {post.get('id')}")

# =========================
# CORE MULTI-SITE DRAFT (JSON + FORM)
# =========================
def create_multisite_draft_internal(site_id: str, title: str, content: str, excerpt: str, topic_key: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            site_url, secret = get_site(cur, site_id)

            # anti-duplicate (sur contenu de base AVANT maillage)
            base_hash = sha256_hex(content)

            cur.execute(
                "SELECT wp_post_id, wp_url FROM articles WHERE site_id = %s AND content_hash = %s",
                (site_id, base_hash),
            )
            dup = cur.fetchone()
            if dup:
                return {"status": "duplicate", "wp_post_id": dup[0], "wp_url": dup[1]}

            # maillage interne par cluster
            internal_block = build_internal_links_block(cur, site_id, topic_key, limit=5)
            if internal_block:
                content = content + "\n\n" + internal_block

            # push WP via plugin (HMAC)
            wp_path = "/wp-json/llmgeo/v1/draft"
            body_json = json.dumps({"title": title, "content": content, "excerpt": excerpt}, ensure_ascii=False)
            wp_json = wp_signed_post(site_url, secret, wp_path, body_json)

            # save DB
            cur.execute(
                """
                INSERT INTO articles (
                    site_id, wp_post_id, wp_status, wp_url,
                    title, content_html, excerpt,
                    topic_key, content_hash, meta
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    site_id,
                    wp_json.get("id"),
                    "draft",
                    wp_json.get("link"),
                    title,
                    content,
                    excerpt,
                    topic_key,
                    base_hash,
                    json.dumps({"source": "engine:draft"}, ensure_ascii=False),
                ),
            )
            conn.commit()

            return {"status": "created", "wp_post_id": wp_json.get("id"), "wp_url": wp_json.get("link")}

@app.post("/sites/{site_id}/draft")
def create_multisite_draft_form(
    site_id: str,
    title: str = Form(...),
    content: str = Form(...),
    excerpt: str = Form(""),
    topic_key: str = Form("general"),
):
    return create_multisite_draft_internal(site_id, title, content, excerpt, topic_key)

@app.post("/api/sites/{site_id}/draft")
def create_multisite_draft_json(site_id: str, payload: DraftIn):
    return create_multisite_draft_internal(
        site_id=site_id,
        title=payload.title,
        content=payload.content_html,
        excerpt=payload.excerpt,
        topic_key=payload.topic_key,
    )

# =========================
# NEW: ANALYZE
# =========================
@app.post("/api/sites/{site_id}/analyze")
def analyze_site(site_id: str):
    """
    Récupère:
      - /site-profile
      - /media?per_page=MAX_MEDIA
    Stocke dans memories:
      - site_profile
      - media_cache
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            site_url, secret = get_site(cur, site_id)

            profile = wp_signed_get(
                site_url=site_url,
                secret=secret,
                call_path="/wp-json/llmgeo/v1/site-profile",
                sign_path="/wp-json/llmgeo/v1/site-profile",
            )

            media = wp_signed_get(
                site_url=site_url,
                secret=secret,
                call_path=f"/wp-json/llmgeo/v1/media?per_page={MAX_MEDIA}",
                sign_path="/wp-json/llmgeo/v1/media",  # signature sans query (ton hmac.php retire la query)
            )

            memory_upsert(cur, site_id, "site_profile", profile)
            memory_upsert(cur, site_id, "media_cache", media)

            conn.commit()

            return {
                "status": "ok",
                "site_id": site_id,
                "profile_loaded": True,
                "media_count": int((media or {}).get("count") or 0),
            }

# =========================
# NEW: TOPICS (deterministic)
# =========================
@app.get("/api/sites/{site_id}/topics")
def topics(site_id: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            profile = memory_get(cur, site_id, "site_profile")
            media = memory_get(cur, site_id, "media_cache")

            if not profile:
                raise HTTPException(status_code=400, detail="Site non analysé. Lance d'abord POST /api/sites/{site_id}/analyze")

            area = normalize_service_area(profile)
            company = get_company_name(profile)

            business = (profile.get("business") or {}) if isinstance(profile, dict) else {}
            category = (business.get("category") or "").strip()
            services = business.get("primary_services") or []
            if not isinstance(services, list):
                services = []

            # fallback services si profil vide => heuristique toiture
            if not services:
                services = [
                    "Nettoyage de toiture",
                    "Démoussage",
                    "Traitement hydrofuge",
                    "Peinture toiture",
                    "Entretien annuel",
                ]

            base_topics = []
            # 1 topic "guide" + 4 topics "intent"
            base_topics.append(("toiture-guide", f"Entretien de toiture à {area} : le guide complet", "guide"))
            for s in services[:8]:
                key = sha256_hex(f"{s}-{area}")[:12]
                base_topics.append((f"svc-{key}", f"{s} à {area} : méthodes, prix et conseils", "service"))

            # Ajout topics basés sur media alt/title
            if isinstance(media, dict):
                items = media.get("items") or []
                hints = []
                for it in items[:10]:
                    txt = f"{it.get('alt','')} {it.get('title','')}".lower()
                    if "peinture" in txt:
                        hints.append(("peinture-toiture", f"Peinture toiture à {area} : étapes, durée, erreurs à éviter", "problem"))
                    if "sale" in txt or "mousse" in txt or "demouss" in txt:
                        hints.append(("demoussage", f"Démoussage toiture à {area} : quand, comment, et quoi vérifier", "tips"))
                # dédoublonne
                seen = set()
                dedup = []
                for t in hints:
                    if t[0] in seen: 
                        continue
                    seen.add(t[0])
                    dedup.append(t)
                base_topics.extend(dedup)

            topics_out = [
                {"topic_key": k, "title": title, "angle": angle, "site": company}
                for (k, title, angle) in base_topics[:20]
            ]
            return {"status": "ok", "site_id": site_id, "topics": topics_out}

# =========================
# NEW: GENERATE DRAFT (deterministic content)
# =========================
@app.post("/api/sites/{site_id}/generate-draft")
def generate_draft(site_id: str, payload: GenerateIn):
    """
    1) charge memories (site_profile + media_cache)
    2) génère article HTML (structure GEO/SEO + images + FAQ JSON-LD)
    3) anti-duplicate via hash
    4) maillage interne (cluster = topic_key)
    5) envoie au plugin WP /draft (HMAC)
    6) stocke dans articles
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            site_url, secret = get_site(cur, site_id)

            profile = memory_get(cur, site_id, "site_profile")
            media = memory_get(cur, site_id, "media_cache")
            if not profile or not media:
                raise HTTPException(status_code=400, detail="Site non analysé. Lance d'abord POST /api/sites/{site_id}/analyze")

            title, content_html, excerpt, images_used = build_article_html(
                profile=profile,
                media_cache=media,
                topic_key=payload.topic_key,
                angle=payload.angle,
            )

            # anti-duplicate sur base content (avant ajout internal links)
            base_hash = sha256_hex(content_html)

            cur.execute(
                "SELECT wp_post_id, wp_url FROM articles WHERE site_id = %s AND content_hash = %s",
                (site_id, base_hash),
            )
            dup = cur.fetchone()
            if dup:
                return {"status": "duplicate", "wp_post_id": dup[0], "wp_url": dup[1]}

            # maillage interne par cluster
            internal_block = build_internal_links_block(cur, site_id, payload.topic_key, limit=5)
            if internal_block:
                content_html = content_html + "\n\n" + internal_block

            # push WP
            wp_path = "/wp-json/llmgeo/v1/draft"
            body_json = json.dumps(
                {"title": title, "content": content_html, "excerpt": excerpt},
                ensure_ascii=False,
            )
            wp_json = wp_signed_post(site_url, secret, wp_path, body_json)

            # save DB
            cur.execute(
                """
                INSERT INTO articles (
                    site_id, wp_post_id, wp_status, wp_url,
                    title, content_html, excerpt,
                    topic_key, content_hash, meta
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
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
                    base_hash,
                    json.dumps(
                        {"source": "engine:generate-draft", "angle": payload.angle, "images_used": images_used},
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()

            return {
                "status": "created",
                "site_id": site_id,
                "topic_key": payload.topic_key,
                "wp_post_id": wp_json.get("id"),
                "wp_url": wp_json.get("link"),
                "images_used": images_used,
            }
