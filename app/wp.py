import base64
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from requests.exceptions import RequestException


class WordPressClient:
    """
    Client WordPress REST API (Application Password).
    Utilisé pour le mode "legacy" (WP_* env) côté engine.
    Pour le mode multi-site via plugin HMAC, on appelle directement requests.post vers /wp-json/llmgeo/v1/*.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        timeout: int = 20,
        user_agent: str = "Mozilla/5.0 (LLM-GEO-Engine; +https://engine.e-ma.re)",
        verify_tls: bool = True,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.app_password = app_password
        self.timeout = timeout
        self.verify_tls = verify_tls

        token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": user_agent,
        }

    def _url(self, path: str) -> str:
        # path peut être "wp-json/wp/v2/posts" ou "/wp-json/wp/v2/posts"
        path = path.lstrip("/")
        return urljoin(self.base_url, path)

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> Dict[str, Any]:
        url = self._url(path)
        try:
            r = requests.request(
                method=method.upper(),
                url=url,
                json=json_body,
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except RequestException as e:
            raise RequestException(f"WordPress request failed: {e}") from e

        # WordPress renvoie parfois des erreurs JSON, parfois du HTML (proxy/WAF).
        if r.status_code >= 400:
            try:
                payload = r.json()
                msg = payload.get("message") or str(payload)
            except Exception:
                msg = r.text[:2000]
            raise RequestException(f"WordPress API error {r.status_code}: {msg}")

        try:
            return r.json()
        except Exception as e:
            raise RequestException(f"Invalid JSON response from WordPress: {e}\nBody: {r.text[:2000]}") from e

    def create_draft_post(self, title: str, content_html: str, excerpt: str = "") -> Dict[str, Any]:
        payload = {
            "title": title,
            "content": content_html,
            "excerpt": excerpt,
            "status": "draft",
        }
        return self._request("POST", "/wp-json/wp/v2/posts", json_body=payload)

    def update_post(self, post_id: int, **fields) -> Dict[str, Any]:
        # fields: title/content/excerpt/status/slug/categories/tags/...
        return self._request("POST", f"/wp-json/wp/v2/posts/{post_id}", json_body=fields)

    def get_post(self, post_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/wp-json/wp/v2/posts/{post_id}")

    def list_posts(self, per_page: int = 10, status: str = "draft") -> Dict[str, Any]:
        # Simple helper (pas de pagination avancée ici)
        return self._request("GET", f"/wp-json/wp/v2/posts?per_page={per_page}&status={status}")
