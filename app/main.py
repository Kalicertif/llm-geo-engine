from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
import os

from app.wp import WordPressClient

app = FastAPI()

# Config via variables d'env (on mettra ensuite un vrai dashboard + DB)
WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip()
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()

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

        <h2>Test : créer un brouillon WordPress</h2>
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
      </body>
    </html>
    """

@app.post("/wp/create-draft", response_class=HTMLResponse)
def create_draft(title: str = Form(...), content: str = Form(...)):
    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        return HTMLResponse("<p>❌ WP non configuré (WP_BASE_URL/WP_USERNAME/WP_APP_PASSWORD)</p>", status_code=400)

    wp = WordPressClient(WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD)
    post = wp.create_draft_post(title=title, content_html=content, excerpt="Brouillon test LLM GEO Engine")
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
