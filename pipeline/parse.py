"""Stage 2: raw pages -> validated ListingCandidate rows + meaningful change detection.

Diff ONLY on the fields below. viewsCount/registerCount change every poll and
are never compared (they'd flag every record as updated daily).
"""
import json

from pydantic import ValidationError

from models import ListingCandidate

from .db import RAW_ROOT, connect


def diff_snapshot(c: ListingCandidate) -> dict:
    return {
        "end_regn_dt": c.regnRequirements.end_regn_dt,   # registration deadline, NOT end_date
        "reg_status": c.regnRequirements.reg_status,
        "prizes": sorted(
            ({"rank": p.rank, "cash": p.cash, "currency": p.currencyCode, "others": p.others}
             for p in c.prizes),
            key=lambda p: json.dumps(p, sort_keys=True)),
        "payment_services": sorted(
            ({"amount": s.amount, "required": s.required} for s in c.payment_services),
            key=lambda s: json.dumps(s, sort_keys=True)),
    }


def load_day(day: str):
    """Yield (page_name, ListingCandidate | None, raw_dict, error) for every record."""
    pages = sorted((RAW_ROOT / day).glob("page_*.json"))
    if not pages:
        raise FileNotFoundError(f"no raw pages for {day} - run fetch first")
    for p in pages:
        for raw in json.loads(p.read_text())["data"]["data"] or []:
            try:
                yield p.name, ListingCandidate.model_validate(raw), raw, None
            except ValidationError as e:
                yield p.name, None, raw, str(e)


def parse_day(day: str) -> dict:
    conn = connect()
    conn.execute("DELETE FROM parse_errors WHERE day = ?", (day,))
    conn.execute("DELETE FROM listing_changes WHERE day = ?", (day,))   # idempotent re-run
    seen, dupes, errors, new, changed = set(), 0, 0, 0, 0

    for page, c, raw, err in load_day(day):
        if err:
            errors += 1
            conn.execute("INSERT INTO parse_errors VALUES (?,?,?,?)", (day, page, raw.get("id"), err))
            continue
        if c.id in seen:
            dupes += 1          # live list shifts while paginating; same record twice
            continue
        seen.add(c.id)
        snap = diff_snapshot(c)
        row = conn.execute("SELECT diff_snapshot, first_seen_day FROM listings WHERE id = ?",
                           (c.id,)).fetchone()
        if row is None:
            new += 1
            first_seen = day
        else:
            first_seen = min(row["first_seen_day"], day)
            old = json.loads(row["diff_snapshot"])
            diffs = [k for k in snap if snap[k] != old.get(k)]
            if diffs and row["first_seen_day"] < day:
                changed += 1
                conn.executemany("INSERT OR REPLACE INTO listing_changes VALUES (?,?,?,?,?)",
                                 [(c.id, day, k, json.dumps(old.get(k)), json.dumps(snap[k])) for k in diffs])
        conn.execute(
            "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?)",
            (c.id, c.organization_id, c.organisation.name, c.type, c.title,
             first_seen, day, json.dumps(snap, sort_keys=True), json.dumps(raw)))

    conn.commit()
    stats = {"unique": len(seen), "duplicates_skipped": dupes, "parse_errors": errors,
             "new": new, "meaningfully_changed": changed}
    print(f"[parse] {day}: {stats}")
    return stats
