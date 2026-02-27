import base64
import requests
from typing import Optional

class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.username = username
        # WP application passwords are used with Basic Auth: user:app_password
        token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("utf-8")
        self.headers = {
            "Authorization": f"Basic {token}",
            "User-Agent": "LLM-GEO-Engine/1.0",
        }
        self.timeout = timeout

    def create_draft_post(
        self,
        title: str,
        content_html: str,
        excerpt: Optional[str] = None,
    ) -> dict:
        url = f"{self.base_url}/wp-json/wp/v2/posts"
        payload = {
            "title": title,
            "content": content_html,
            "status": "draft",
        }
        if excerpt:
            payload["excerpt"] = excerpt

        r = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
