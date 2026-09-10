"""Stage 6: the whole daily run against Supabase - what GitHub Actions executes.

  1. pull pipeline.db from the private `pipeline` storage bucket (fresh start if none)
  2. pull reviewer decisions -> decisions / org_reputation / enrichment_log
     (before scoring, so today's scores already use yesterday's verdicts)
  3. fetch -> parse -> score -> enrich
  4. upsert queue + archive_items, delete rows for closed listings, update sync_meta
  5. always: push pipeline.db back, and archive the day's raw JSON + logs as a tarball

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment. The
service-role key bypasses RLS: it must only ever live in GitHub secrets.
"""
import io
import os
import tarfile
from datetime import datetime

import httpx

from .db import DB_PATH, LOG_ROOT, RAW_ROOT, connect
from .enrich import enrich_day
from .fetch import fetch_day
from .parse import parse_day
from .portal import collect, import_rows
from .score import CURRENT, score_day

BUCKET = "pipeline"
STATE_KEY = "state/pipeline.db"
UPSERT = {"Prefer": "resolution=merge-duplicates,return=minimal"}


def _client() -> httpx.Client:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return httpx.Client(base_url=url, timeout=60,
                        headers={"apikey": key, "Authorization": f"Bearer {key}"})


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r


def pull_state(c) -> bool:
    r = c.get(f"/storage/v1/object/{BUCKET}/{STATE_KEY}")
    if r.status_code in (400, 404) and "not found" in r.text.lower():
        print("[cloud] no saved state - starting fresh (today becomes the baseline day)")
        return False
    DB_PATH.write_bytes(_check(r).content)   # any other error aborts: never overwrite real state
    print(f"[cloud] pulled state ({DB_PATH.stat().st_size // 1024} KB)")
    return True


def push_state(c) -> None:
    _check(c.post(f"/storage/v1/object/{BUCKET}/{STATE_KEY}", content=DB_PATH.read_bytes(),
                  headers={"x-upsert": "true", "content-type": "application/octet-stream"}))
    print("[cloud] pushed state")


def archive_day(c, day: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if (RAW_ROOT / day).exists():
            tar.add(RAW_ROOT / day, arcname=f"raw/{day}")
        for p in LOG_ROOT.rglob(f"{day}*"):
            tar.add(p, arcname=str(p.relative_to(LOG_ROOT.parent)))
    _check(c.post(f"/storage/v1/object/{BUCKET}/archives/{day}.tar.gz", content=buf.getvalue(),
                  headers={"x-upsert": "true", "content-type": "application/gzip"}))
    print(f"[cloud] archived raw + logs ({len(buf.getvalue()) // 1024} KB)")


def pull_decisions(c) -> dict:
    rows = _check(c.get("/rest/v1/decisions", params={
        "select": "listing_id,decision,reason,decided_at,card"})).json()
    return import_rows([{"id": r["listing_id"], "decision": r["decision"], "reason": r["reason"],
                         "decided_at": r["decided_at"], "org_id": (r["card"] or {}).get("org_id")}
                        for r in rows])


def _replace(c, table: str, rows: list) -> None:
    """Upsert `rows` and delete every other row, so the table mirrors today."""
    for i in range(0, len(rows), 500):
        _check(c.post(f"/rest/v1/{table}", params={"on_conflict": "id"},
                      json=rows[i:i + 500], headers=UPSERT))
    ids = ",".join(str(r["id"]) for r in rows)
    _check(c.delete(f"/rest/v1/{table}", params={"id": f"not.in.({ids})" if ids else "gt.0"}))


def push_portal(c, day: str) -> dict:
    queue, archive = collect(day, connect())
    _replace(c, "queue", [{"id": q["id"], "priority": q["priority"], "deadline": q["deadline"],
                           "data": q} for q in queue])
    _replace(c, "archive_items", [{"id": a["id"], "first_seen": a["first_seen"], "data": a}
                                  for a in archive])
    counts = {"queue": len(queue), "archive": len(archive)}
    _check(c.post("/rest/v1/sync_meta", params={"on_conflict": "id"}, headers=UPSERT, json={
        "id": 1, "data": {"day": day, "rules_version": CURRENT, **counts,
                          "exported_at": datetime.now().astimezone().isoformat(timespec="seconds")}}))
    print(f"[cloud] portal updated: {counts}")
    return counts


def run_cloud(day: str) -> None:
    with _client() as c:
        pull_state(c)
        connect().close()                      # make sure the schema exists on a fresh start
        pull_decisions(c)
        try:
            fetch_day(day)
            parse_day(day)
            score_day(day)
            enrich_day(day)
            push_portal(c, day)
        finally:                               # keep state even if a stage failed midway
            push_state(c)
            archive_day(c, day)
