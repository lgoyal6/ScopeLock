"""SQLite persistence, WAL mode, single file. Pure stdlib, no telephony."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from scopelock.core.redact import redact

DB_PATH = Path(__file__).resolve().parent.parent.parent / "scopelock.db"

_local = threading.local()

CASE_STATES = (
    "intake",
    "mandate_draft",
    "mandated",
    "awaiting_institution",
    "connecting_holder",
    "representing",
    "consulting_holder",
    "closing",
    "closed",
    "interrupted",
)

DISPOSITIONS = ("allowed", "requires_holder", "forbidden")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kase (
  id            TEXT PRIMARY KEY,
  holder_name   TEXT NOT NULL,
  holder_lang   TEXT NOT NULL,
  holder_phone  TEXT,
  institution   TEXT NOT NULL,
  issue_type    TEXT NOT NULL,
  issue_summary TEXT NOT NULL,
  session_a     TEXT,
  session_b     TEXT,
  state         TEXT NOT NULL CHECK(state IN (
                  'intake', 'mandate_draft', 'mandated', 'awaiting_institution',
                  'connecting_holder', 'representing', 'consulting_holder',
                  'closing', 'closed', 'interrupted'
                )),
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandate_rule (
  id          TEXT PRIMARY KEY,
  case_id     TEXT NOT NULL REFERENCES kase(id),
  verb        TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('allowed', 'requires_holder', 'forbidden')),
  confirmed_by_holder INTEGER DEFAULT 0,
  confirmed_utterance TEXT,
  UNIQUE(case_id, verb)
);

CREATE TABLE IF NOT EXISTS policy_event (
  id               TEXT PRIMARY KEY,
  case_id          TEXT NOT NULL,
  verb             TEXT NOT NULL,
  disposition      TEXT NOT NULL,
  source           TEXT NOT NULL,
  trigger_redacted TEXT,
  result           TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_request (
  id          TEXT PRIMARY KEY,
  case_id     TEXT NOT NULL,
  verb        TEXT NOT NULL,
  question_en TEXT NOT NULL,
  question_es TEXT NOT NULL,
  answer_es   TEXT,
  answer_en   TEXT,
  status      TEXT NOT NULL,
  decided_by  TEXT CHECK(decided_by IS NULL OR decided_by = 'holder'),
  latency_ms  INTEGER,
  created_at  TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS violation (
  id         TEXT PRIMARY KEY,
  case_id    TEXT NOT NULL,
  verb       TEXT NOT NULL,
  trigger    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utterance (
  id         TEXT PRIMARY KEY,
  case_id    TEXT NOT NULL,
  leg        TEXT NOT NULL,
  speaker    TEXT NOT NULL,
  lang       TEXT NOT NULL,
  text       TEXT NOT NULL,
  call_id    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authz_seq (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandate_version (
  id            TEXT PRIMARY KEY,
  case_id       TEXT NOT NULL REFERENCES kase(id),
  version       INTEGER NOT NULL,
  rules_json    TEXT NOT NULL,
  confirmed_at  REAL NOT NULL,
  expires_at    REAL,
  created_seq   INTEGER NOT NULL,
  revoked_at    REAL,
  revoked_seq   INTEGER,
  revoke_reason TEXT,
  UNIQUE(case_id, version)
);

CREATE TABLE IF NOT EXISTS action_effect (
  id                  TEXT PRIMARY KEY,
  case_id             TEXT NOT NULL REFERENCES kase(id),
  verb                TEXT NOT NULL,
  mandate_version     INTEGER NOT NULL,
  idempotency_key     TEXT NOT NULL,
  decision_request_id TEXT UNIQUE,
  source              TEXT NOT NULL,
  trigger_redacted    TEXT,
  correlation_id      TEXT,
  seq                 INTEGER NOT NULL,
  created_at          TEXT NOT NULL,
  UNIQUE(case_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS card (
  case_id      TEXT PRIMARY KEY REFERENCES kase(id),
  asked        TEXT NOT NULL,
  told         TEXT NOT NULL,
  reference_no TEXT,
  agreed       TEXT,
  refused      TEXT NOT NULL,
  next_step    TEXT,
  created_at   TEXT NOT NULL
);
"""


# Columns added after the original schema shipped. ``CREATE TABLE IF NOT EXISTS`` does not
# alter an existing file, so connect() adds any that are missing (additive, idempotent).
_ADDED_COLUMNS = {
    "decision_request": {
        "cancel_reason": "TEXT",
        "created_seq": "INTEGER",
    },
}

# Injectable wall clock for mandate expiry; tests monkeypatch this.
_clock = time.time


def epoch() -> float:
    return float(_clock())


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for column, decl in columns.items():
            if column not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {decl}')


def next_seq(conn: sqlite3.Connection, case_id: str, kind: str) -> int:
    """Allocate a monotonic ledger sequence number inside the caller's transaction."""
    cursor = conn.execute(
        "INSERT INTO authz_seq (case_id, kind, created_at) VALUES (?, ?, ?)",
        (case_id, kind, now()),
    )
    return int(cursor.lastrowid)


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):013x}{uuid.uuid4().hex[:12]}"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _local.conn = conn
    return conn


def create_case(holder_name: str, holder_lang: str, institution: str,
                 issue_type: str, issue_summary: str, holder_phone: str | None = None) -> str:
    case_id = new_id("cas")
    conn = connect()
    conn.execute(
        "INSERT INTO kase (id, holder_name, holder_lang, holder_phone, institution, issue_type, "
        "issue_summary, session_a, session_b, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'intake', ?)",
        (case_id, holder_name, holder_lang, holder_phone, institution, issue_type, issue_summary, now()),
    )
    conn.commit()
    return case_id


def get_case(case_id: str) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM kase WHERE id = ?", (case_id,)).fetchone()


def get_open_case() -> sqlite3.Row | None:
    """Return the newest confirmed case waiting for its institution leg."""
    return connect().execute(
        "SELECT * FROM kase WHERE state IN ('mandated', 'awaiting_institution') "
        "AND session_b IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def set_case_state(case_id: str, state: str) -> None:
    # Compatibility for the untouched relay module while keeping the persisted model
    # inside the explicit state machine from build-spec section 8.1.
    state = "awaiting_institution" if state == "live" else state
    if state not in CASE_STATES:
        raise ValueError(f"invalid case state: {state}")
    conn = connect()
    conn.execute("UPDATE kase SET state = ? WHERE id = ?", (state, case_id))
    conn.commit()


def set_case_session(case_id: str, *, session_a: str | None = None, session_b: str | None = None) -> None:
    conn = connect()
    if session_a is not None:
        conn.execute("UPDATE kase SET session_a = ? WHERE id = ?", (session_a, case_id))
    if session_b is not None:
        conn.execute("UPDATE kase SET session_b = ? WHERE id = ?", (session_b, case_id))
    conn.commit()


def create_mandate_rules(case_id: str, rules: dict[str, str | bool]) -> None:
    """Replace a case's draft rules.

    Boolean values remain accepted only as a compatibility seam for callers from the
    baseline branch; all persisted values use the frozen tri-state contract.
    """
    normalized = {
        verb: ("allowed" if value else "forbidden") if isinstance(value, bool) else value
        for verb, value in rules.items()
    }
    invalid = set(normalized.values()) - set(DISPOSITIONS)
    if invalid:
        raise ValueError(f"invalid mandate disposition(s): {sorted(invalid)}")
    conn = connect()
    conn.execute("DELETE FROM mandate_rule WHERE case_id = ?", (case_id,))
    conn.executemany(
        "INSERT INTO mandate_rule (id, case_id, verb, disposition, confirmed_by_holder, confirmed_utterance) "
        "VALUES (?, ?, ?, ?, 0, NULL)",
        [(new_id("man"), case_id, verb, disposition) for verb, disposition in normalized.items()],
    )
    conn.commit()


def mandate_rules(case_id: str) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM mandate_rule WHERE case_id = ? ORDER BY verb", (case_id,)
    ).fetchall()


def mandate_rule(case_id: str, verb: str) -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM mandate_rule WHERE case_id = ? AND verb = ?", (case_id, verb)
    ).fetchone()


def set_mandate_disposition(case_id: str, verb: str, disposition: str) -> None:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"invalid mandate disposition: {disposition}")
    conn = connect()
    cursor = conn.execute(
        "UPDATE mandate_rule SET disposition = ? WHERE case_id = ? AND verb = ?",
        (disposition, case_id, verb),
    )
    if cursor.rowcount != 1:
        raise KeyError(f"no mandate rule for case {case_id} and verb {verb}")
    conn.commit()


def confirm_mandate(case_id: str, utterance: str) -> None:
    """A single verbal confirmation covers the whole mandate — see spec 5.3."""
    conn = connect()
    conn.execute(
        "UPDATE mandate_rule SET confirmed_by_holder = 1, confirmed_utterance = ? WHERE case_id = ?",
        (redact(utterance), case_id),
    )
    conn.commit()


def latest_mandate_version(case_id: str,
                           conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    return (conn or connect()).execute(
        "SELECT * FROM mandate_version WHERE case_id = ? ORDER BY version DESC LIMIT 1",
        (case_id,),
    ).fetchone()


def mandate_version_status(case_id: str, conn: sqlite3.Connection | None = None) -> str:
    """'none' (never confirmed), 'active', 'revoked' or 'expired' for the latest version."""
    row = latest_mandate_version(case_id, conn)
    if row is None:
        return "none"
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and epoch() >= row["expires_at"]:
        return "expired"
    return "active"


def mandate_confirmed(case_id: str) -> bool:
    """Confirmed means every rule was confirmed aloud AND the confirmed version is live.

    A revoked or expired version no longer counts, so the relay gate and health checks
    close with it.
    """
    rows = mandate_rules(case_id)
    if not (bool(rows) and all(r["confirmed_by_holder"] for r in rows)):
        return False
    return mandate_version_status(case_id) in ("none", "active")


def log_violation(case_id: str, verb: str, trigger: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO violation (id, case_id, verb, trigger, created_at) VALUES (?, ?, ?, ?, ?)",
        (new_id("vio"), case_id, verb, redact(trigger), now()),
    )
    conn.commit()


def violations(case_id: str) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM violation WHERE case_id = ? ORDER BY created_at", (case_id,)
    ).fetchall()


def record_consult(case_id: str, question_en: str, question_es: str,
                    answer_es: str, answer_en: str, latency_ms: int | None = None) -> str:
    """Compatibility writer backed by the sole decision_request ledger."""
    conn = connect()
    consult_id = new_id("dec")
    conn.execute(
        "INSERT INTO decision_request (id, case_id, verb, question_en, question_es, answer_es, "
        "answer_en, status, decided_by, latency_ms, created_at, resolved_at) "
        "VALUES (?, ?, 'holder_decision', ?, ?, ?, ?, 'resolved', 'holder', ?, ?, ?)",
        (
            consult_id,
            case_id,
            redact(question_en),
            redact(question_es),
            redact(answer_es),
            redact(answer_en),
            latency_ms,
            now(),
            now(),
        ),
    )
    conn.commit()
    return consult_id


def consults(case_id: str) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM decision_request WHERE case_id = ? AND status = 'resolved' "
        "ORDER BY created_at",
        (case_id,),
    ).fetchall()


def add_utterance(case_id: str, leg: str, speaker: str, lang: str, text: str, call_id: str,
                   external_id: str | None = None) -> None:
    """external_id dedupes progressive caller-speech updates (same utterance_id, growing text)
    by upserting in place instead of appending a new row per partial transcript."""
    conn = connect()
    row_id = f"utt_{leg}_{external_id}" if external_id else new_id("utt")
    conn.execute(
        "INSERT INTO utterance (id, case_id, leg, speaker, lang, text, call_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET text = excluded.text",
        (row_id, case_id, leg, speaker, lang, redact(text), call_id, now()),
    )
    conn.commit()


def utterances(case_id: str) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM utterance WHERE case_id = ? ORDER BY created_at", (case_id,)
    ).fetchall()


def upsert_card(case_id: str, *, asked: list[str] | None = None, told: list[str] | None = None,
                 reference_no: str | None = None, agreed: str | None = None,
                 next_step: str | None = None) -> None:
    conn = connect()
    row = conn.execute("SELECT * FROM card WHERE case_id = ?", (case_id,)).fetchone()
    refused = [v["verb"] for v in violations(case_id)]
    if row is None:
        conn.execute(
            "INSERT INTO card (case_id, asked, told, reference_no, agreed, refused, next_step, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, json.dumps(asked or []), json.dumps(told or []), reference_no, agreed,
             json.dumps(refused), next_step, now()),
        )
    else:
        merged_asked = json.loads(row["asked"]) + (asked or [])
        merged_told = json.loads(row["told"]) + (told or [])
        conn.execute(
            "UPDATE card SET asked = ?, told = ?, reference_no = COALESCE(?, reference_no), "
            "agreed = COALESCE(?, agreed), refused = ?, next_step = COALESCE(?, next_step) "
            "WHERE case_id = ?",
            (json.dumps(merged_asked), json.dumps(merged_told), reference_no, agreed,
             json.dumps(refused), next_step, case_id),
        )
    conn.commit()


def get_card(case_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM card WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    return {
        "case_id": row["case_id"],
        "asked": json.loads(row["asked"]),
        "told": json.loads(row["told"]),
        "reference_no": row["reference_no"],
        "agreed": row["agreed"],
        "refused": json.loads(row["refused"]),
        "next_step": row["next_step"],
    }


def reset_db() -> None:
    """Test helper: drop and recreate all tables."""
    conn = connect()
    conn.executescript("""
        DROP TABLE IF EXISTS kase;
        DROP TABLE IF EXISTS mandate_rule;
        DROP TABLE IF EXISTS policy_event;
        DROP TABLE IF EXISTS decision_request;
        DROP TABLE IF EXISTS consult;
        DROP TABLE IF EXISTS violation;
        DROP TABLE IF EXISTS utterance;
        DROP TABLE IF EXISTS card;
        DROP TABLE IF EXISTS authz_seq;
        DROP TABLE IF EXISTS mandate_version;
        DROP TABLE IF EXISTS action_effect;
    """)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
