"""Stage 4 (Tier B): fetch ENDPOINT 2 for new or meaningfully-changed competitions.

Scope (decided 2026-09-10): every type=competitions listing first seen after the
baseline day (the first fetch - no backfill of the ~200 already open then), plus
any competition whose deadline/status/prizes/fee changed today. Results are
cached in enrichment_log and raw JSON is archived under raw/<day>/enrich/, so a
listing seen again tomorrow is not re-fetched.

Signals written here are UNVALIDATED - shown to Isha as context, never scored.
"""
import json
import time

import httpx

from models import CompetitionDetail

from .db import RAW_ROOT, connect
from .fetch import DELAY_S, HEADERS, _get

DETAIL_URL = "https://unstop.com/api/public/competition/{id}"


def _targets(conn, day: str) -> list:
    baseline = conn.execute("SELECT MIN(first_seen_day) FROM listings").fetchone()[0]
    rows = conn.execute("""
        SELECT l.id FROM listings l
        WHERE l.type = 'competitions' AND l.last_seen_day = :day AND (
              (l.first_seen_day = :day AND l.first_seen_day > :baseline
               AND l.id NOT IN (SELECT listing_id FROM enrichment_log))
           OR l.id IN (SELECT listing_id FROM listing_changes WHERE day = :day))
        ORDER BY l.id""", {"day": day, "baseline": baseline}).fetchall()
    return [r["id"] for r in rows]


def enrich_day(day: str, ids: list = None) -> dict:
    conn = connect()
    ids = ids or _targets(conn, day)
    out_dir = RAW_ROOT / day / "enrich"
    out_dir.mkdir(parents=True, exist_ok=True)
    done, failed = 0, []

    with httpx.Client(headers=HEADERS, timeout=30) as client:
        for i, lid in enumerate(ids):
            raw_path = out_dir / f"{lid}.json"
            try:
                if not raw_path.exists():          # idempotent: reuse today's fetch
                    if i:
                        time.sleep(DELAY_S)
                    raw_path.write_text(_get(client, DETAIL_URL.format(id=lid)).text)
                data = json.loads(raw_path.read_text())["data"]
                det = CompetitionDetail.model_validate(data.get("competition", data))
            except Exception as e:                  # one bad record must not stop the day
                failed.append({"id": lid, "error": str(e)[:200]})
                continue
            f = det.enrichment_fields()
            conn.execute("""
                INSERT INTO enrichment_log (listing_id, enriched_day, organisation_type, web_url,
                                            attachment, rounds_count, account_tag)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    enriched_day=excluded.enriched_day, organisation_type=excluded.organisation_type,
                    web_url=excluded.web_url, attachment=excluded.attachment,
                    rounds_count=excluded.rounds_count, account_tag=excluded.account_tag""",
                (lid, day, f["organisation_type"], f["web_url"], json.dumps(f["attachment"]),
                 f["rounds_count"], f["account_tag"]))
            done += 1
    conn.commit()
    stats = {"targets": len(ids), "enriched": done, "failed": failed}
    print(f"[enrich] {day}: {stats}")
    return stats
