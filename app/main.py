from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os

app = FastAPI()

@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
    }

@app.get("/", response_class=HTMLResponse)
def home():
    # Mini dashboard provisoire (on l’enrichira ensuite)
    return """
    <html>
      <head><title>LLM GEO Engine</title></head>
      <body style="font-family: sans-serif; margin: 2rem;">
        <h1>LLM GEO Engine</h1>
        <p>✅ API en ligne.</p>
        <ul>
          <li><a href="/health">/health</a></li>
        </ul>
      </body>
    </html>
    """
