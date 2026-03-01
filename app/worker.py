import os
import time
import json
import hashlib
import requests

try:
    import redis  # type: ignore
except Exception:
    redis = None  # noqa


ENGINE_BASE_URL = os.getenv("ENGINE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ENGINE_ADMIN_TOKEN = os.getenv("ENGINE_ADMIN_TOKEN", "").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
SLEEP_SECONDS = int(os.getenv("WORKER_SLEEP_SECONDS", "60"))

AUTO_SITES_JSON = os.getenv("AUTO_SITES_JSON", "[]")

REDIS_URL = os.getenv("REDIS_URL", "").strip()


def _freq_to_seconds(freq: str) -> int:
    freq = (freq or "").strip().lower()
    if freq == "1_per_day":
        return 24 * 3600
    if freq == "1_per_week":
        return 7 * 24 * 3600
    if freq == "1_per_month":
        return 30 * 24 * 3600
    # fallback safe
    return 7 * 24 * 3600


def _key(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self):
        self.mem = {}
        self.r = None
        if REDIS_URL and redis is not None:
            try:
                self.r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
                self.r.ping()
                print("[worker] redis connected", flush=True)
            except Exception as e:
                print(f"[worker] redis disabled: {e}", flush=True)
                self.r = None

    def get_ts(self, k: str) -> int:
        if self.r:
            v = self.r.get(k)
            return int(v) if v else 0
        return int(self.mem.get(k, 0))

    def set_ts(self, k: str, ts: int):
        if self.r:
            self.r.set(k, str(ts))
            return
        self.mem[k] = ts


def _headers() -> dict:
    if ENGINE_ADMIN_TOKEN:
        return {"X-Engine-Token": ENGINE_ADMIN_TOKEN}
    return {}


def call_analyze(site_id: str) -> bool:
    url = f"{ENGINE_BASE_URL}/api/sites/{site_id}/analyze"
    r = requests.post(url, headers=_headers(), timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
        print(f"[worker] analyze failed {site_id}: {r.status_code} {r.text[:200]}", flush=True)
        return False
    print(f"[worker] analyze ok {site_id}", flush=True)
    return True


def call_generate(site_id: str, topic_key: str, frequency: str, images_count: int) -> bool:
    url = f"{ENGINE_BASE_URL}/api/sites/{site_id}/generate-draft"
    payload = {
        "topic_key": topic_key,
        "frequency": frequency,
        "images_count": images_count,
    }
    r = requests.post(url, headers=_headers(), json=payload, timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
        print(f"[worker] generate failed {site_id}/{topic_key}: {r.status_code} {r.text[:200]}", flush=True)
        return False
    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text[:200]}
    print(f"[worker] generate ok {site_id}/{topic_key}: {out}", flush=True)
    return True


def load_jobs() -> list[dict]:
    try:
        jobs = json.loads(AUTO_SITES_JSON)
        if isinstance(jobs, list):
            return jobs
    except Exception as e:
        print(f"[worker] invalid AUTO_SITES_JSON: {e}", flush=True)
    return []


def main():
    env = os.getenv("ENVIRONMENT", "unknown")
    print(f"[worker] started (ENVIRONMENT={env}) base={ENGINE_BASE_URL}", flush=True)

    store = StateStore()
    jobs = load_jobs()
    if not jobs:
        print("[worker] no jobs configured (AUTO_SITES_JSON is empty). Worker will idle.", flush=True)

    while True:
        now = int(time.time())
        jobs = load_jobs()  # reload live (Coolify env changes => redeploy usually, but safe)

        # 1) Daily analyze per site
        seen_sites = set()
        for j in jobs:
            site_id = str(j.get("site_id", "")).strip()
            if not site_id or site_id in seen_sites:
                continue
            seen_sites.add(site_id)

            k_an = "analyze:" + _key(site_id)
            last = store.get_ts(k_an)
            if now - last >= 24 * 3600:
                if call_analyze(site_id):
                    store.set_ts(k_an, now)

        # 2) Generate drafts per job (frequency-based)
        for j in jobs:
            site_id = str(j.get("site_id", "")).strip()
            topic_key = str(j.get("topic_key", "")).strip()
            frequency = str(j.get("frequency", "1_per_week")).strip()
            images_count = int(j.get("images_count", 2))

            if not site_id or not topic_key:
                continue

            every = _freq_to_seconds(frequency)
            k_gen = "gen:" + _key(site_id, topic_key)
            last = store.get_ts(k_gen)
            if now - last >= every:
                ok = call_generate(site_id, topic_key, frequency, images_count)
                if ok:
                    store.set_ts(k_gen, now)

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
