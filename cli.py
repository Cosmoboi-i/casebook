"""Unstop competition pipeline. Each stage is independently runnable and idempotent.

  python cli.py fetch   [--day YYYY-MM-DD] [--force]
  python cli.py parse   [--day ...]
  python cli.py score   [--day ...] [--rules v1|v2|v3]
  python cli.py enrich  [--day ...] [--ids 123,456]   # Tier B, ENDPOINT 2
  python cli.py export  [--day ...]                   # -> outbox/<day>/ for the portal
  python cli.py import  --dir <read_db out_dir>/decisions
  python cli.py report  [--day ...]
  python cli.py run     [--day ...]      # fetch + parse + score + enrich + export + report

Portal sync (done by Claude, the portal can't reach this machine):
  push:  Artifact write_db db_op=batch, one call per outbox/<day>/batch_NNN.json
  pull:  Artifact read_db collection=decisions out_dir=<dir>, then `cli.py import`
"""
import argparse
import json
from collections import Counter
from datetime import date

from pipeline.db import connect
from pipeline.enrich import enrich_day
from pipeline.fetch import fetch_day
from pipeline.parse import parse_day
from pipeline.portal import export_day, import_decisions
from pipeline.score import CURRENT, RULESETS, score_day


def report(day: str) -> None:
    conn = connect()
    rows = conn.execute("""
        SELECT s.score, s.bucket, s.breakdown, s.rules_version, l.id, l.type, l.title, l.org_name
        FROM scores s JOIN listings l ON l.id = s.listing_id
        WHERE s.day = ? ORDER BY s.score DESC, l.id""", (day,)).fetchall()
    if not rows:
        print(f"no scores for {day}")
        return

    print(f"\n=== Tier A report {day} (rules {rows[0]['rules_version']}): {len(rows)} unique candidates ===")
    by = Counter((r["bucket"], r["type"]) for r in rows)
    types = sorted({r["type"] for r in rows})
    print(f"{'bucket':<16}" + "".join(f"{t:>14}" for t in types) + f"{'total':>8}")
    for b in ("enrich_pending", "review", "archive"):
        print(f"{b:<16}" + "".join(f"{by[(b, t)]:>14}" for t in types)
              + f"{sum(by[(b, t)] for t in types):>8}")
    print("score distribution:", dict(sorted(Counter(r["score"] for r in rows).items(), reverse=True)))

    for b in ("enrich_pending", "review"):
        sel = [r for r in rows if r["bucket"] == b]
        print(f"\n--- {b} ({len(sel)}) ---")
        for r in sel:
            rules = ",".join(f"{k}{p:+d}" for k, p in json.loads(r["breakdown"]))
            print(f"{r['score']:>3}  {r['id']}  {r['type'][:5]:<5}  {r['title'][:58]:<58}  "
                  f"{(r['org_name'] or '')[:34]:<34}  [{rules}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["fetch", "parse", "score", "enrich", "export", "import",
                                      "report", "run", "cloud"])
    ap.add_argument("--day", default=date.today().isoformat())
    ap.add_argument("--force", action="store_true", help="re-fetch even if day is archived")
    ap.add_argument("--rules", choices=sorted(RULESETS), default=CURRENT,
                    help="scoring ruleset; v1 = original spec (fallback)")
    ap.add_argument("--ids", help="enrich only these listing ids (comma-separated)")
    ap.add_argument("--dir", help="import: directory of decision docs from the portal")
    a = ap.parse_args()

    if a.stage == "cloud":                      # the GitHub Actions daily run
        from pipeline.cloud import run_cloud
        run_cloud(a.day)
        return
    if a.stage == "import":
        if not a.dir:
            ap.error("import needs --dir")
        import_decisions(a.dir)
        return
    if a.stage in ("fetch", "run"):
        fetch_day(a.day, force=a.force)
    if a.stage in ("parse", "run"):
        parse_day(a.day)
    if a.stage in ("score", "run"):
        score_day(a.day, a.rules)
    if a.stage in ("enrich", "run"):
        enrich_day(a.day, [int(i) for i in a.ids.split(",")] if a.ids else None)
    if a.stage in ("export", "run"):
        export_day(a.day)
    if a.stage in ("report", "run"):
        report(a.day)


if __name__ == "__main__":
    main()
