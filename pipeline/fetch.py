"""Stage 1: fetch ENDPOINT 1, follow next_page_url until null, archive raw pages.

Idempotent: a day with a _complete.json marker is skipped unless force=True.
Raw pages are stored verbatim so parse/score can be re-run without re-fetching.
"""
import json
import time
from datetime import datetime

import httpx

from .db import RAW_ROOT

BASE_URL = "https://unstop.com/api/public/opportunity/search-result"
# domain/course are supported by the API but deliberately omitted for now:
# we have no validated values for them, and a wrong value silently narrows results.
PARAMS = {
    "opportunity": "competitions",   # does NOT filter by type server-side
    "oppstatus": "open",
    "usertype": "students",
    "per_page": "50",
    "page": "1",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (mba-comp-feed)", "Accept": "application/json"}
MAX_PAGES = 200          # loop guard; ~10 pages expected
DELAY_S = 1.0            # be polite; throughput is not a goal


def _get(client: httpx.Client, url: str, params=None) -> httpx.Response:
    for attempt in range(1, 4):
        try:
            r = client.get(url, params=params)
            if r.status_code == 200:
                return r
            if r.status_code not in (429, 500, 502, 503, 504):
                r.raise_for_status()
        except httpx.TransportError:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    r.raise_for_status()
    return r


def fetch_day(day: str, force: bool = False) -> dict:
    day_dir = RAW_ROOT / day
    marker = day_dir / "_complete.json"
    if marker.exists() and not force:
        info = json.loads(marker.read_text())
        print(f"[fetch] {day} already fetched ({info['pages']} pages, {info['records']} records) - skipping")
        return info

    day_dir.mkdir(parents=True, exist_ok=True)
    for old in day_dir.glob("page_*.json"):
        old.unlink()

    seen_urls, pages, records, total = set(), 0, 0, None
    url, params = BASE_URL, PARAMS
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        while url:
            if pages >= MAX_PAGES:
                raise RuntimeError(f"exceeded {MAX_PAGES} pages - pagination loop?")
            r = _get(client, url, params)
            pages += 1
            (day_dir / f"page_{pages:03d}.json").write_text(r.text)
            data = r.json()["data"]
            n = len(data.get("data") or [])
            records += n
            total = data.get("total", total)
            nxt = data.get("next_page_url")
            print(f"[fetch] page {pages}: {n} records (reported total {total})")
            if nxt and nxt in seen_urls:
                raise RuntimeError(f"next_page_url repeated: {nxt}")
            seen_urls.add(nxt)
            url, params = nxt, None      # next_page_url already carries the query
            if url:
                time.sleep(DELAY_S)

    info = {"day": day, "pages": pages, "records": records, "reported_total": total,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}
    marker.write_text(json.dumps(info, indent=2))
    return info
