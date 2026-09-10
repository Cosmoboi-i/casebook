"""Stage 3: Tier A scoring on every ENDPOINT 1 candidate.

Rules are versioned so we can fall back: `python cli.py score --rules v1`.
Re-scoring a day snapshots its previous scores to logs/score_snapshots/ first.

v1 (original spec) - failed its first full-day test on 2026-09-10: 129/406 to
   enrichment, and the blocklist on `details` killed real case comps.
v2 - title-only blocklist, +2 case signal, no_all +2, enrich >= 4. Better enrich
   list (85) but single words in details ("strategy") let robotics in, and
   184 went to review - too many for Isha.
v3 (current):
  +2  filters does NOT contain {id: 420, name: "All"}
  +2  assignedTag.tag == "d2c-trusted"
  +1  organisation.tier is not null
  +2  case signal: CASE_PHRASES in title/details, or CASE_WORDS in title only
  +3  org has a net-approved history in org_reputation (still goes to Isha)
  -2  org has a net-rejected history
  -3  TITLE matches BLOCKLIST
  -1  type == "quizzes"
  GATE: no case signal, not d2c-trusted, not a known-good org -> archive.
  score >= 4 -> enrich_pending | <= 0 -> archive | else -> review

Every regex here is invented, not validated. For the first week every match is
written UNFILTERED to logs/{blocklist_rejections,case_signal_matches}/<day>.<ver>.jsonl,
and gated records are visible to Isha in the portal's Archive tab.
"""
import html
import json
import re
from datetime import datetime

from models import ListingCandidate

from .db import LOG_ROOT, connect
from .parse import load_day

BLOCKLIST = re.compile(
    r"\b(MCQ|Quiz|Hackathon|Buildathon|Ideathon|Line Follower|Truss|CAD|Workshop)\b",
    re.IGNORECASE)
# v2: one list, matched anywhere.
CASE_SIGNAL = re.compile(
    r"\b(case (?:study|studies|competition|challenge|event)|b-?plan|business plan|"
    r"consulting|strategy|M&A|mergers|guesstimate|marketing|finance|brand|"
    r"product management|human resources)\b",
    re.IGNORECASE)
# v3: phrases are specific enough for details; bare words only count in titles.
CASE_PHRASES = re.compile(
    r"\b(case (?:study|studies|competition|challenge|event)|b-?plan|business plan|"
    r"consulting|M&A|mergers|guesstimate|product management|human resources)\b",
    re.IGNORECASE)
CASE_WORDS = re.compile(r"\b(strategy|strategic|marketing|finance|brand|HR)\b", re.IGNORECASE)

RULESETS = {
    "v1": dict(no_all=3, d2c=2, tier=1, case_signal=0, blocklist=-3, quizzes=-1,
               blocklist_scope=("title", "details"), enrich_at=3, archive_at=0),
    "v2": dict(no_all=2, d2c=2, tier=1, case_signal=2, case_mode="any", blocklist=-3,
               quizzes=-1, blocklist_scope=("title",), enrich_at=4, archive_at=0),
    "v3": dict(no_all=2, d2c=2, tier=1, case_signal=2, case_mode="split", blocklist=-3,
               quizzes=-1, blocklist_scope=("title",), enrich_at=4, archive_at=0,
               org_good=3, org_bad=-2, require_signal=True),
}
CURRENT = "v3"


def _text(html_str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", html_str or ""))


def _search(pattern, fields):
    for where, text in fields:
        m = pattern.search(text)
        if m:
            s, e = max(m.start() - 50, 0), min(m.end() + 50, len(text))
            return {"where": where, "term": m.group(0), "context": " ".join(text[s:e].split())}
    return None


def _case_hit(fields: dict, rules: dict):
    if not rules["case_signal"]:
        return None
    if rules.get("case_mode") == "split":
        return (_search(CASE_PHRASES, fields.items())
                or _search(CASE_WORDS, [("title", fields["title"])]))
    return _search(CASE_SIGNAL, fields.items())


def bucket_for(score: int, rules: dict, gated: bool = False) -> str:
    if gated or score <= rules["archive_at"]:
        return "archive"
    if score >= rules["enrich_at"]:
        return "enrich_pending"
    return "review"


def score_candidate(c: ListingCandidate, rules: dict, reputation: dict = None):
    """Return (score, bucket, breakdown, hits). Pure given `reputation`
    ({organisation_id: approvals - rejections})."""
    fields = {"title": c.title, "details": _text(c.details)}
    breakdown = []
    if not any(f.id == 420 and f.name == "All" for f in c.filters):
        breakdown.append(("no_all_filter", rules["no_all"]))
    d2c = c.assignedTag.tag == "d2c-trusted"
    if d2c:
        breakdown.append(("d2c_trusted", rules["d2c"]))
    if c.organisation.tier is not None:
        breakdown.append(("org_tier", rules["tier"]))

    net = (reputation or {}).get(c.organization_id, 0)
    known_good = bool(rules.get("org_good")) and net > 0
    if known_good:
        breakdown.append(("known_good_org", rules["org_good"]))
    elif rules.get("org_bad") and net < 0:
        breakdown.append(("known_bad_org", rules["org_bad"]))

    case_hit = _case_hit(fields, rules)
    if case_hit:
        breakdown.append((f"case_signal:{case_hit['term']}", rules["case_signal"]))
    block_hit = _search(BLOCKLIST, [(k, fields[k]) for k in rules["blocklist_scope"]])
    if block_hit:
        breakdown.append((f"blocklist:{block_hit['term']}", rules["blocklist"]))
    if c.type == "quizzes":
        breakdown.append(("type_quizzes", rules["quizzes"]))

    gated = bool(rules.get("require_signal")) and not (case_hit or d2c or known_good)
    if gated:
        breakdown.append(("gate:no_content_signal", 0))
    score = sum(p for _, p in breakdown)
    hits = {"blocklist": block_hit, "case_signal": case_hit,
            "gated_without_case": bool(rules.get("require_signal")) and not (d2c or known_good)}
    return score, bucket_for(score, rules, gated), breakdown, hits


def load_reputation(conn) -> dict:
    return {r["organisation_id"]: r["approvals"] - r["rejections"]
            for r in conn.execute("SELECT organisation_id, approvals, rejections FROM org_reputation")}


def _snapshot_existing(conn, day: str) -> None:
    rows = conn.execute("""
        SELECT s.*, l.type, l.title, l.org_name FROM scores s
        JOIN listings l ON l.id = s.listing_id WHERE s.day = ?""", (day,)).fetchall()
    if not rows:
        return
    ver = rows[0]["rules_version"]
    path = LOG_ROOT / "score_snapshots" / f"{day}.{ver}.jsonl"
    if path.exists():      # never overwrite an earlier snapshot of the same version
        path = path.with_name(f"{day}.{ver}.{datetime.now():%H%M%S}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps({**dict(r), "breakdown": json.loads(r["breakdown"])}) + "\n")
    print(f"[score] snapshotted {len(rows)} previous {ver} scores -> {path.relative_to(LOG_ROOT.parent)}")


def score_day(day: str, version: str = CURRENT) -> dict:
    rules = RULESETS[version]
    conn = connect()
    reputation = load_reputation(conn)
    _snapshot_existing(conn, day)
    conn.execute("DELETE FROM scores WHERE day = ?", (day,))

    logs = {}
    for kind in ("blocklist_rejections", "case_signal_matches"):
        p = LOG_ROOT / kind / f"{day}.{version}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        logs[kind] = p.open("w")

    seen, counts = set(), {"enrich_pending": 0, "review": 0, "archive": 0}
    for _, c, _, err in load_day(day):
        if err or c.id in seen:
            continue
        seen.add(c.id)
        score, bucket, breakdown, hits = score_candidate(c, rules, reputation)
        counts[bucket] += 1
        conn.execute("INSERT INTO scores VALUES (?,?,?,?,?,?)",
                     (day, c.id, score, bucket, json.dumps(breakdown), version))
        base = {"id": c.id, "title": c.title, "org": c.organisation.name, "type": c.type,
                "url": c.seo_url, "score": score, "bucket": bucket}
        for kind, key in (("blocklist_rejections", "blocklist"), ("case_signal_matches", "case_signal")):
            hit = hits[key]
            if hit:
                without = score - rules[key]
                gated = key == "case_signal" and hits["gated_without_case"]
                logs[kind].write(json.dumps({**base, **hit, "score_without_rule": without,
                                             "bucket_without_rule": bucket_for(without, rules, gated)}) + "\n")
    for f in logs.values():
        f.close()
    conn.commit()
    print(f"[score] {day} rules={version}: {counts}")
    return counts
