"""Builds what reviewers and students see, and reads reviewer decisions back.

  collect(day)        -> queue cards + archive items for the day (shared by both portals)
  export_day(day)     -> legacy Claude-artifact portal: JSON files + write_db batch manifests
  import_rows(rows)   -> decisions, org_reputation (rebuilt), enrichment_log.decision
  import_decisions()  -> import_rows() from files saved by Artifact read_db

The Supabase portal uses collect()/import_rows() via pipeline/cloud.py.
Score and rule breakdown are deliberately never exported: reviewers see raw
context values, never the algorithm's verdict.
"""
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from models import ListingCandidate

from .db import ROOT, connect

OUTBOX = ROOT / "outbox"
ARCHIVE_DAYS = 7
BATCH = 50
FIELD_LABELS = {"end_regn_dt": "Registration deadline", "reg_status": "Registration status",
                "prizes": "Prizes", "payment_services": "Entry fee"}


def _inr(amount) -> str:
    """Indian digit grouping: 200000 -> 2,00,000."""
    s = str(int(round(amount)))
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + groups + [tail])


def _money(amount, code) -> str:
    if code in (None, "INR"):
        return f"₹{_inr(amount)}"
    return f"{code} {amount:,.0f}"


def prize_line(c: ListingCandidate) -> str:
    cash = [p for p in c.prizes if p.cash]
    if cash:
        top = max(cash, key=lambda p: p.cash)
        line = f"{_money(sum(p.cash for p in cash), top.currencyCode)} in prizes"
        if len(cash) > 1 and top.rank:
            line += f" · {top.rank} {_money(top.cash, top.currencyCode)}"
        return line
    other = next((p.others for p in c.prizes if p.others), None)
    return other[:60] if other else ("Certificates only" if c.prizes else "No prizes listed")


def fee_line(c: ListingCandidate) -> str:
    fees = [s.amount for s in c.payment_services if s.amount and s.required]
    return f"{_money(max(fees), 'INR')} entry fee" if fees else "Free"


def eligibility_line(c: ListingCandidate) -> str:
    names = [f.name for f in c.filters]
    if any(f.id == 420 for f in c.filters) or not names:
        who = "Open to all students"
    elif len(names) > 4:
        who = ", ".join(names[:3]) + f" +{len(names) - 3} more"
    else:
        who = ", ".join(names)
    lo, hi = c.regnRequirements.min_team_size, c.regnRequirements.max_team_size
    team = "Solo" if hi == 1 else (f"Team of {lo}–{hi}" if lo and hi and lo != hi else f"Team of {hi or lo}")
    return f"{who} · {team}"


def _card(c: ListingCandidate) -> dict:
    return {"id": c.id, "title": c.title, "org": c.organisation.name, "org_id": c.organization_id,
            "type": c.type, "url": c.seo_url, "deadline": c.regnRequirements.end_regn_dt,
            "reg_status": c.regnRequirements.reg_status, "prizes": prize_line(c),
            "fee": fee_line(c), "eligibility": eligibility_line(c)}


def _archive_reason(breakdown: list) -> str:
    for rule, _ in breakdown:
        if rule.startswith("blocklist:"):
            return f"Blocklisted word in title: “{rule.split(':', 1)[1]}”"
    if any(rule == "gate:no_content_signal" for rule, _ in breakdown):
        return "No case-competition signal"
    return "Low score"


def collect(day: str, conn=None):
    """Return (queue_cards, archive_items) for listings still open on `day`."""
    conn = conn or connect()
    enr = {r["listing_id"]: dict(r) for r in conn.execute("SELECT * FROM enrichment_log")}
    changes = {}
    for r in conn.execute("SELECT * FROM listing_changes WHERE day = ?", (day,)):
        changes.setdefault(r["listing_id"], []).append(
            {"field": FIELD_LABELS.get(r["field"], r["field"]),
             "from": json.loads(r["old_value"]), "to": json.loads(r["new_value"])})

    rows = conn.execute("""
        SELECT l.raw_json, l.first_seen_day, s.bucket, s.breakdown FROM listings l
        JOIN scores s ON s.listing_id = l.id AND s.day = :day
        WHERE l.last_seen_day = :day""", {"day": day}).fetchall()
    archive_from = (date.fromisoformat(day) - timedelta(days=ARCHIVE_DAYS - 1)).isoformat()
    queue, archive = [], []
    for r in rows:
        c = ListingCandidate.model_validate(json.loads(r["raw_json"]))
        if r["bucket"] in ("enrich_pending", "review") or c.id in changes:
            e = enr.get(c.id)
            queue.append({
                **_card(c),
                "priority": 1 if r["bucket"] == "enrich_pending" else 2,   # sort only, never shown
                "first_seen": r["first_seen_day"],
                "changed": changes.get(c.id, []),
                "changed_at": day if c.id in changes else None,
                "context": {
                    "d2c_trusted": c.assignedTag.tag == "d2c-trusted",
                    "tier": c.organisation.tier,
                    "enriched": e is not None,
                    "organisation_type": e and e["organisation_type"],
                    "web_url": e and e["web_url"],
                    "rounds": e and e["rounds_count"],
                    "attachments": e and len(json.loads(e["attachment"] or "[]")),
                    "account_tag": e and e["account_tag"],
                },
            })
        elif r["first_seen_day"] >= archive_from:
            archive.append({**_card(c), "first_seen": r["first_seen_day"],
                            "reason": _archive_reason(json.loads(r["breakdown"]))})
    return queue, archive


def export_day(day: str) -> dict:
    """Legacy: files for the Claude-artifact portal (Artifact write_db batches)."""
    conn = connect()
    out = OUTBOX / day
    shutil.rmtree(out, ignore_errors=True)
    queue, archive = collect(day, conn)
    docs = {("queue", str(q["id"])): q for q in queue}
    by_day = {}
    for a in archive:
        by_day.setdefault(a["first_seen"], []).append(a)
    for d, items in by_day.items():
        docs[("archive", d)] = {"day": d, "items": sorted(items, key=lambda a: a["title"])}

    prev = {(r["collection"], r["doc_id"]) for r in conn.execute("SELECT * FROM portal_docs")}
    deletes = sorted(prev - set(docs))
    counts = {"queue": len(queue), "archive": len(archive), "deletes": len(deletes)}
    docs[("meta", "sync")] = {"day": day, "exported_at": datetime.now().isoformat(timespec="seconds"),
                              **counts}

    writes = []
    for (coll, did), body in docs.items():
        p = out / "docs" / coll / f"{did}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, ensure_ascii=False))
        writes.append({"op": "set", "collection": coll, "doc_id": did, "file_path": str(p)})
    writes += [{"op": "delete", "collection": coll, "doc_id": did} for coll, did in deletes]
    batches = [writes[i:i + BATCH] for i in range(0, len(writes), BATCH)]
    for n, b in enumerate(batches, 1):
        (out / f"batch_{n:03d}.json").write_text(json.dumps({"writes": b}, indent=1))

    conn.execute("DELETE FROM portal_docs")
    conn.executemany("INSERT INTO portal_docs VALUES (?,?,?)",
                     [(coll, did, day) for coll, did in docs if coll != "meta"])
    conn.commit()
    print(f"[export] {day}: {counts}, {len(writes)} writes in {len(batches)} batches -> {out.relative_to(ROOT)}")
    return {**counts, "batches": len(batches)}


def import_rows(rows: list, conn=None) -> dict:
    """rows: [{id, decision, reason, decided_at, org_id}] from either portal."""
    conn = conn or connect()
    n = 0
    for d in rows:
        if d.get("decision") not in ("approve", "reject", "skip"):
            continue
        conn.execute("""
            INSERT INTO decisions VALUES (?,?,?,?,?)
            ON CONFLICT(listing_id) DO UPDATE SET decision=excluded.decision,
                reject_reason=excluded.reject_reason, decided_at=excluded.decided_at,
                organisation_id=excluded.organisation_id
            WHERE excluded.decided_at >= decisions.decided_at""",
            (int(d["id"]), d["decision"], d.get("reason"), d["decided_at"], d.get("org_id")))
        n += 1

    conn.execute("DELETE FROM org_reputation")
    conn.execute("""
        INSERT INTO org_reputation
        SELECT d.organisation_id, MAX(l.org_name),
               SUM(d.decision = 'approve'), SUM(d.decision = 'reject'), MAX(d.decided_at)
        FROM decisions d LEFT JOIN listings l ON l.id = d.listing_id
        WHERE d.decision IN ('approve', 'reject') AND d.organisation_id IS NOT NULL
        GROUP BY d.organisation_id""")
    conn.execute("""
        UPDATE enrichment_log SET
            decision = (SELECT decision FROM decisions WHERE listing_id = enrichment_log.listing_id),
            reject_reason = (SELECT reject_reason FROM decisions WHERE listing_id = enrichment_log.listing_id),
            decided_at = (SELECT decided_at FROM decisions WHERE listing_id = enrichment_log.listing_id)
        WHERE listing_id IN (SELECT listing_id FROM decisions WHERE decision IN ('approve', 'reject'))""")
    conn.commit()
    stats = {"decisions_read": n,
             "orgs": conn.execute("SELECT COUNT(*) FROM org_reputation").fetchone()[0]}
    print(f"[import] {stats}")
    return stats


def import_decisions(src: Path) -> dict:
    """src: directory of decision docs as saved by Artifact read_db out_dir."""
    rows = []
    for f in sorted(Path(src).glob("*.json")):
        d = json.loads(f.read_text())
        if "decision" not in d and isinstance(d.get("data"), dict):
            d = d["data"]
        rows.append({**d, "id": d.get("id") or f.stem})
    return import_rows(rows)
