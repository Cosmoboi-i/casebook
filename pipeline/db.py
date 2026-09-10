"""SQLite storage. One file, no server - matches the low-maintenance goal."""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pipeline.db"
RAW_ROOT = ROOT / "raw"
LOG_ROOT = ROOT / "logs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id              INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL,
    org_name        TEXT,
    type            TEXT,
    title           TEXT,
    first_seen_day  TEXT NOT NULL,
    last_seen_day   TEXT NOT NULL,
    diff_snapshot   TEXT NOT NULL,   -- JSON of the meaningful-diff fields only
    raw_json        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_changes (
    listing_id INTEGER NOT NULL,
    day        TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    PRIMARY KEY (listing_id, day, field)
);
CREATE TABLE IF NOT EXISTS parse_errors (
    day TEXT NOT NULL, page TEXT NOT NULL, record_id INTEGER, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    day        TEXT NOT NULL,
    listing_id INTEGER NOT NULL,
    score      INTEGER NOT NULL,
    bucket     TEXT NOT NULL,        -- enrich_pending / review / archive
    breakdown  TEXT NOT NULL,        -- JSON list of [rule, points]
    rules_version TEXT NOT NULL DEFAULT 'v1',
    PRIMARY KEY (day, listing_id)
);
-- Tier B. One row per enriched listing. The five signal columns are
-- UNVALIDATED: displayed to Isha as context, never scored. Revisit ~30 days in.
CREATE TABLE IF NOT EXISTS enrichment_log (
    listing_id        INTEGER PRIMARY KEY,
    enriched_day      TEXT NOT NULL,
    organisation_type TEXT,
    web_url           TEXT,
    attachment        TEXT,            -- JSON list
    rounds_count      INTEGER,
    account_tag       TEXT,            -- assignTags.tag (organiser account)
    decision          TEXT,            -- approve / reject / NULL (never reviewed)
    reject_reason     TEXT,
    decided_at        TEXT
);
-- Isha's decisions, pulled back from the portal. Latest decision per listing wins.
CREATE TABLE IF NOT EXISTS decisions (
    listing_id    INTEGER PRIMARY KEY,
    decision      TEXT NOT NULL,       -- approve / reject / skip
    reject_reason TEXT,
    decided_at    TEXT NOT NULL,
    organisation_id INTEGER
);
-- Rebuilt from `decisions` on every import (skips don't count).
CREATE TABLE IF NOT EXISTS org_reputation (
    organisation_id INTEGER PRIMARY KEY,
    org_name   TEXT,
    approvals  INTEGER NOT NULL DEFAULT 0,
    rejections INTEGER NOT NULL DEFAULT 0,
    last_decided_at TEXT
);
-- What the last export put in the portal, so closed listings get deleted there.
CREATE TABLE IF NOT EXISTS portal_docs (
    collection TEXT NOT NULL, doc_id TEXT NOT NULL, last_exported_day TEXT NOT NULL,
    PRIMARY KEY (collection, doc_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    if "rules_version" not in cols:   # DBs created before rules were versioned
        conn.execute("ALTER TABLE scores ADD COLUMN rules_version TEXT NOT NULL DEFAULT 'v1'")
    return conn
