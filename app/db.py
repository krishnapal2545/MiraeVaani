"""SQLite storage for agents, credentials and call history.

Deliberately plain sqlite3: queries here are sub-millisecond CRUD, so the
blocking calls cost far less than the machinery needed to avoid them. If this
grows into the multi-tenant platform, this module is what gets swapped for
Postgres + SQLAlchemy.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.models import AgentConfig

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Every row is scoped to an org so the multi-tenant split is later a change to
# queries rather than a migration. Credentials additionally use '' for the
# platform-owned key that an org falls back to when it has not brought its own.
DEFAULT_ORG = "default"
PLATFORM_ORG = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    org_id      TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL,
    secret_enc  BLOB NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (org_id, provider)
);

CREATE TABLE IF NOT EXISTS agents (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    stt_provider           TEXT NOT NULL,
    stt_model              TEXT NOT NULL,
    language_mode          TEXT NOT NULL,
    language               TEXT NOT NULL,
    llm_provider           TEXT NOT NULL,
    llm_model              TEXT NOT NULL,
    temperature            REAL NOT NULL,
    max_output_tokens      INTEGER NOT NULL,
    tts_provider           TEXT NOT NULL,
    tts_voice              TEXT NOT NULL,
    system_prompt          TEXT NOT NULL,
    greeting_mode          TEXT NOT NULL,
    greeting_text          TEXT NOT NULL,
    fillers_enabled        INTEGER NOT NULL,
    filler_delay_ms        INTEGER NOT NULL,
    silence_threshold_rms  INTEGER NOT NULL,
    silence_end_seconds    REAL NOT NULL,
    min_utterance_seconds  REAL NOT NULL,
    redirect_number        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    to_number       TEXT NOT NULL,
    call_sid        TEXT,
    direction       TEXT NOT NULL DEFAULT 'outbound',
    status          TEXT NOT NULL,
    variables       TEXT NOT NULL DEFAULT '{}',
    started_at      TEXT,
    ended_at        TEXT,
    duration_s      REAL,
    turns           INTEGER DEFAULT 0,
    outcome         TEXT,
    outcome_summary TEXT,
    log_dir         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_sid ON calls(call_sid);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at DESC);

CREATE TABLE IF NOT EXISTS call_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id       TEXT NOT NULL,
    turn          INTEGER NOT NULL,
    role          TEXT NOT NULL,
    text          TEXT NOT NULL,
    language      TEXT,
    stt_ms        INTEGER,
    llm_ms        INTEGER,
    tts_ttfb_ms   INTEGER,
    total_ms      INTEGER,
    filler_played TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_call ON call_turns(call_id, turn);

CREATE TABLE IF NOT EXISTS orgs (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    max_concurrent_calls INTEGER NOT NULL DEFAULT 5,
    timezone             TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_lists (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL DEFAULT 'default',
    name            TEXT NOT NULL,
    source_filename TEXT NOT NULL DEFAULT '',
    contact_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    list_id    TEXT NOT NULL,
    org_id     TEXT NOT NULL DEFAULT 'default',
    phone_e164 TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    variables  TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
-- Dedup is enforced by the database, so a re-uploaded CSV cannot double-dial.
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_unique ON contacts(list_id, phone_e164);

CREATE TABLE IF NOT EXISTS campaigns (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL DEFAULT 'default',
    agent_id            TEXT NOT NULL,
    list_id             TEXT NOT NULL,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',
    window_start        TEXT NOT NULL DEFAULT '10:00',
    window_end          TEXT NOT NULL DEFAULT '19:00',
    days                TEXT NOT NULL DEFAULT '0,1,2,3,4,5',
    timezone            TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    max_concurrent      INTEGER NOT NULL DEFAULT 2,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    retry_after_minutes INTEGER NOT NULL DEFAULT 120,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- One row per contact per campaign, materialised when the campaign first
-- starts. This is what makes a campaign resumable: the worker asks the table
-- what is still pending rather than holding a queue in memory, so a restart
-- (or a crash, or Ctrl+C) loses nothing.
CREATE TABLE IF NOT EXISTS campaign_targets (
    id              TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    org_id          TEXT NOT NULL DEFAULT 'default',
    contact_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_call_id    TEXT,
    outcome         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_claim
    ON campaign_targets(agent_id, status, next_attempt_at);
-- Makes materialising a campaign idempotent: starting it twice cannot enqueue
-- the same contact twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_unique
    ON campaign_targets(campaign_id, contact_id);

CREATE TABLE IF NOT EXISTS suppressions (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL DEFAULT 'default',
    phone_e164 TEXT NOT NULL,
    agent_id   TEXT,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suppressions_phone ON suppressions(org_id, phone_e164);

-- The worker process registry. Rows outlive the process on purpose: a parent
-- that was killed reads this back at startup to find and reap orphans that are
-- still dialing real numbers.
CREATE TABLE IF NOT EXISTS workers (
    agent_id       TEXT PRIMARY KEY,
    pid            INTEGER NOT NULL,
    port           INTEGER NOT NULL,
    status         TEXT NOT NULL,
    config_version INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL
);
"""

_AGENT_FIELDS = [
    "name", "stt_provider", "stt_model", "language_mode", "language",
    "allowed_languages", "language_switch_turns", "language_switch_min_seconds",
    "llm_provider", "llm_model", "temperature", "max_output_tokens",
    "tts_provider", "tts_voice", "tts_speaking_rate", "tts_pitch", "tts_pause_ms",
    "system_prompt", "greeting_mode", "greeting_text",
    "fillers_enabled", "filler_delay_ms",
    "silence_threshold_rms", "silence_end_seconds", "min_utterance_seconds",
    "noise_margin", "barge_in_seconds", "barge_in_grace_seconds",
    "no_reply_seconds", "no_reply_prompts",
    "redirect_number",
    "max_concurrent_calls", "outcome_webhook_url",
]

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op
# on an existing database, so a schema change that is not also listed here is a
# schema change that only works on a fresh install. Each entry is
# (column, SQL type, default) and is applied idempotently at connect time.
_AGENT_MIGRATIONS: list[tuple[str, str, Any]] = [
    ("allowed_languages", "TEXT", ""),
    ("language_switch_turns", "INTEGER", 2),
    ("language_switch_min_seconds", "REAL", 1.0),
    ("tts_speaking_rate", "REAL", 0.95),
    ("tts_pitch", "REAL", 0.0),
    ("tts_pause_ms", "INTEGER", 350),
    ("noise_margin", "REAL", 2.0),
    ("barge_in_seconds", "REAL", 0.5),
    ("barge_in_grace_seconds", "REAL", 0.7),
    ("no_reply_seconds", "REAL", 6.0),
    ("no_reply_prompts", "INTEGER", 2),
    ("org_id", "TEXT", DEFAULT_ORG),
    ("max_concurrent_calls", "INTEGER", 20),
    ("outcome_webhook_url", "TEXT", ""),
    # Bumped on every edit. A worker holds the config it loaded at startup, so
    # this is how the management app knows a running worker is now stale.
    ("config_version", "INTEGER", 0),
]

_CALL_MIGRATIONS: list[tuple[str, str, Any]] = [
    ("org_id", "TEXT", DEFAULT_ORG),
    ("campaign_id", "TEXT", ""),
    ("attempt", "INTEGER", 1),
]


def _add_columns(
    conn: sqlite3.Connection, table: str, migrations: list[tuple[str, str, Any]]
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, sql_type, default in migrations:
        if column in existing:
            continue
        literal = f"'{default}'" if isinstance(default, str) else str(default)
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {sql_type} "
            f"NOT NULL DEFAULT {literal}"
        )


def _migrate_credentials(conn: sqlite3.Connection) -> None:
    """Re-key credentials from `provider` to `(org_id, provider)`.

    An org that brings its own Sarvam key has to be able to override the
    platform's, which needs a composite primary key — and SQLite cannot ALTER
    one. Rebuilding is therefore the only route. Existing rows become the
    platform key (org_id='') that every org falls back to, so nothing that
    worked before this migration stops working after it.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(credentials)")}
    if "org_id" in columns:
        return
    conn.executescript(
        """
        ALTER TABLE credentials RENAME TO credentials_old;
        CREATE TABLE credentials (
            org_id      TEXT NOT NULL DEFAULT '',
            provider    TEXT NOT NULL,
            secret_enc  BLOB NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (org_id, provider)
        );
        INSERT INTO credentials(org_id, provider, secret_enc, updated_at)
            SELECT '', provider, secret_enc, updated_at FROM credentials_old;
        DROP TABLE credentials_old;
        """
    )


def _migrate(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "agents", _AGENT_MIGRATIONS)
    _add_columns(conn, "calls", _CALL_MIGRATIONS)
    _migrate_credentials(conn)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(get_settings().DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # Each agent's worker is its own process writing call turns throughout
        # every call. The default rollback journal takes a database-wide lock
        # per write, so two live agents deadlock into "database is locked"
        # almost immediately; WAL lets readers and one writer proceed together,
        # and busy_timeout makes concurrent writers wait their turn instead of
        # failing outright.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def init() -> None:
    connect()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _credential_scopes(org_id: str) -> list[str]:
    """Where to look for a key, most specific first.

    This ordering is the whole of the hybrid credential model: an org that has
    brought its own key uses it, and one that has not falls back to the
    platform's.
    """
    return [org_id, PLATFORM_ORG] if org_id != PLATFORM_ORG else [PLATFORM_ORG]


def set_credential(provider: str, secret: str, org_id: str = PLATFORM_ORG) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO credentials(org_id, provider, secret_enc, updated_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(org_id, provider) DO UPDATE SET "
            "secret_enc=excluded.secret_enc, updated_at=excluded.updated_at",
            (org_id, provider, encrypt(secret), _now()),
        )
        connect().commit()


def get_credential(provider: str, org_id: str = PLATFORM_ORG) -> str:
    for scope in _credential_scopes(org_id):
        row = connect().execute(
            "SELECT secret_enc FROM credentials WHERE org_id=? AND provider=?",
            (scope, provider),
        ).fetchone()
        if row:
            return decrypt(row["secret_enc"])
    return ""


def get_credential_json(provider: str, org_id: str = PLATFORM_ORG) -> dict[str, Any]:
    """For providers whose credential is a JSON blob (twilio: sid/token/number)."""
    raw = get_credential(provider, org_id)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def set_credential_json(
    provider: str, data: dict[str, Any], org_id: str = PLATFORM_ORG
) -> None:
    set_credential(provider, json.dumps(data), org_id)


def list_credential_providers(org_id: str = PLATFORM_ORG) -> list[str]:
    """Providers this org can use, counting what it inherits from the platform."""
    scopes = _credential_scopes(org_id)
    placeholders = ",".join("?" * len(scopes))
    rows = connect().execute(
        f"SELECT DISTINCT provider FROM credentials WHERE org_id IN ({placeholders})",
        scopes,
    )
    return [r["provider"] for r in rows]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
def _row_to_agent(row: sqlite3.Row) -> AgentConfig:
    data = {k: row[k] for k in _AGENT_FIELDS}
    data["id"] = row["id"]
    data["fillers_enabled"] = bool(row["fillers_enabled"])
    return AgentConfig(**data)


def create_agent(agent: AgentConfig) -> AgentConfig:
    agent.id = agent.id or uuid.uuid4().hex[:12]
    values = [getattr(agent, f) for f in _AGENT_FIELDS]
    with _lock:
        connect().execute(
            f"INSERT INTO agents(id, {', '.join(_AGENT_FIELDS)}, created_at, updated_at) "
            f"VALUES({', '.join('?' * (len(_AGENT_FIELDS) + 3))})",
            [agent.id, *values, _now(), _now()],
        )
        connect().commit()
    return agent


def update_agent(agent_id: str, agent: AgentConfig) -> AgentConfig | None:
    if get_agent(agent_id) is None:
        return None
    assignments = ", ".join(f"{f}=?" for f in _AGENT_FIELDS)
    values = [getattr(agent, f) for f in _AGENT_FIELDS]
    with _lock:
        # A worker holds the config it read at startup, so every edit has to
        # move the version it can compare itself against.
        connect().execute(
            f"UPDATE agents SET {assignments}, "
            f"config_version=config_version+1, updated_at=? WHERE id=?",
            [*values, _now(), agent_id],
        )
        connect().commit()
    agent.id = agent_id
    return agent


def get_agent(agent_id: str) -> AgentConfig | None:
    row = connect().execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return _row_to_agent(row) if row else None


def list_agents() -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT id, name, stt_provider, llm_provider, llm_model, tts_provider, "
        "tts_voice, language_mode, language, updated_at FROM agents ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_agent(agent_id: str) -> None:
    with _lock:
        connect().execute("DELETE FROM agents WHERE id=?", (agent_id,))
        connect().commit()


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------
def create_call(call_id: str, agent_id: str, to_number: str, variables: dict) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO calls(id, agent_id, to_number, status, variables, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (call_id, agent_id, to_number, "queued", json.dumps(variables), _now()),
        )
        connect().commit()


def update_call(call_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        connect().execute(
            f"UPDATE calls SET {assignments} WHERE id=?", [*fields.values(), call_id]
        )
        connect().commit()


def get_call(call_id: str) -> dict[str, Any] | None:
    row = connect().execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    return dict(row) if row else None


def find_call_by_sid(call_sid: str) -> dict[str, Any] | None:
    row = connect().execute(
        "SELECT * FROM calls WHERE call_sid=?", (call_sid,)
    ).fetchone()
    return dict(row) if row else None


def list_calls(limit: int = 50, agent_id: str | None = None) -> list[dict[str, Any]]:
    """Recent calls, newest first — for one agent's own log when `agent_id` is set."""
    sql = (
        "SELECT c.*, a.name AS agent_name FROM calls c "
        "LEFT JOIN agents a ON a.id = c.agent_id "
    )
    params: list[Any] = []
    if agent_id:
        sql += "WHERE c.agent_id = ? "
        params.append(agent_id)
    sql += "ORDER BY c.created_at DESC LIMIT ?"
    params.append(limit)
    rows = connect().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def add_turn(call_id: str, turn: int, role: str, text: str, **metrics: Any) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO call_turns(call_id, turn, role, text, language, stt_ms, "
            "llm_ms, tts_ttfb_ms, total_ms, filler_played, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                call_id, turn, role, text,
                metrics.get("language"), metrics.get("stt_ms"), metrics.get("llm_ms"),
                metrics.get("tts_ttfb_ms"), metrics.get("total_ms"),
                metrics.get("filler_played"), _now(),
            ),
        )
        connect().commit()


def list_turns(call_id: str) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM call_turns WHERE call_id=? ORDER BY turn, id", (call_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Contact lists
# ---------------------------------------------------------------------------
def create_contact_list(
    name: str, source_filename: str = "", org_id: str = DEFAULT_ORG
) -> str:
    list_id = uuid.uuid4().hex[:12]
    with _lock:
        connect().execute(
            "INSERT INTO contact_lists(id, org_id, name, source_filename, created_at) "
            "VALUES(?,?,?,?,?)",
            (list_id, org_id, name, source_filename, _now()),
        )
        connect().commit()
    return list_id


def insert_contacts(rows: list[tuple]) -> int:
    """Rows are (id, list_id, org_id, phone_e164, name, variables_json).

    OR IGNORE leans on the unique index so re-uploading a file that overlaps an
    existing list adds only what is new instead of duplicating the dial list.
    Returns how many rows were actually stored.
    """
    if not rows:
        return 0
    now = _now()
    with _lock:
        cursor = connect().executemany(
            "INSERT OR IGNORE INTO contacts"
            "(id, list_id, org_id, phone_e164, name, variables, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(*row, now) for row in rows],
        )
        inserted = cursor.rowcount
        connect().execute(
            "UPDATE contact_lists SET contact_count="
            "(SELECT count(*) FROM contacts WHERE list_id=contact_lists.id) "
            "WHERE id=?",
            (rows[0][1],),
        )
        connect().commit()
    return inserted


def list_contact_lists(org_id: str = DEFAULT_ORG) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM contact_lists WHERE org_id=? ORDER BY created_at DESC",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_contact_list(list_id: str) -> dict[str, Any] | None:
    row = connect().execute(
        "SELECT * FROM contact_lists WHERE id=?", (list_id,)
    ).fetchone()
    return dict(row) if row else None


def list_contacts(list_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM contacts WHERE list_id=? ORDER BY created_at, id LIMIT ?",
        (list_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_contact_list(list_id: str) -> None:
    with _lock:
        connect().execute("DELETE FROM contacts WHERE list_id=?", (list_id,))
        connect().execute("DELETE FROM contact_lists WHERE id=?", (list_id,))
        connect().commit()


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
_CAMPAIGN_FIELDS = [
    "agent_id", "list_id", "name", "window_start", "window_end", "days",
    "timezone", "max_concurrent", "max_attempts", "retry_after_minutes",
]


def create_campaign(data: dict[str, Any], org_id: str = DEFAULT_ORG) -> str:
    campaign_id = uuid.uuid4().hex[:12]
    values = [data.get(f) for f in _CAMPAIGN_FIELDS]
    with _lock:
        connect().execute(
            f"INSERT INTO campaigns(id, org_id, {', '.join(_CAMPAIGN_FIELDS)}, "
            f"created_at, updated_at) "
            f"VALUES({', '.join('?' * (len(_CAMPAIGN_FIELDS) + 4))})",
            [campaign_id, org_id, *values, _now(), _now()],
        )
        connect().commit()
    return campaign_id


def update_campaign(campaign_id: str, data: dict[str, Any]) -> None:
    fields = {k: v for k, v in data.items() if k in _CAMPAIGN_FIELDS}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        connect().execute(
            f"UPDATE campaigns SET {assignments}, updated_at=? WHERE id=?",
            [*fields.values(), _now(), campaign_id],
        )
        connect().commit()


def set_campaign_status(campaign_id: str, status: str) -> None:
    with _lock:
        connect().execute(
            "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
            (status, _now(), campaign_id),
        )
        connect().commit()


def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    row = connect().execute(
        "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
    ).fetchone()
    return dict(row) if row else None


def list_campaigns(org_id: str = DEFAULT_ORG) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT c.*, a.name AS agent_name, l.name AS list_name "
        "FROM campaigns c "
        "LEFT JOIN agents a ON a.id = c.agent_id "
        "LEFT JOIN contact_lists l ON l.id = c.list_id "
        "WHERE c.org_id=? ORDER BY c.created_at DESC",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def running_campaigns(agent_id: str) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM campaigns WHERE agent_id=? AND status='running'", (agent_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Campaign targets — the work list
# ---------------------------------------------------------------------------
def materialise_targets(campaign: dict[str, Any]) -> int:
    """Enqueue one target per contact. Safe to call again on the same campaign.

    OR IGNORE against the (campaign_id, contact_id) index means restarting a
    paused campaign adds only contacts that were added to the list since, and
    never re-enqueues someone who has already been called.
    """
    now = _now()
    with _lock:
        cursor = connect().execute(
            "INSERT OR IGNORE INTO campaign_targets"
            "(id, campaign_id, agent_id, org_id, contact_id, status, "
            " next_attempt_at, created_at) "
            "SELECT lower(hex(randomblob(8))), ?, ?, ?, id, 'pending', ?, ? "
            "FROM contacts WHERE list_id=?",
            (campaign["id"], campaign["agent_id"], campaign["org_id"], now, now,
             campaign["list_id"]),
        )
        connect().commit()
    return cursor.rowcount


def claim_targets(agent_id: str, limit: int) -> list[dict[str, Any]]:
    """Take up to `limit` contacts that are due, marking them as being dialed.

    Marked before returning, not after dialing: a crash in between leaves rows
    stuck in `dialing`, which is recoverable, rather than rows still `pending`
    that a restart would dial a second time.
    """
    if limit <= 0:
        return []
    now = _now()
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT t.*, c.phone_e164, c.name AS contact_name, "
            "       c.variables AS contact_variables "
            "FROM campaign_targets t "
            "JOIN contacts c ON c.id = t.contact_id "
            "JOIN campaigns cam ON cam.id = t.campaign_id "
            "WHERE t.agent_id=? AND t.status='pending' AND t.next_attempt_at <= ? "
            "      AND cam.status='running' "
            "ORDER BY t.next_attempt_at LIMIT ?",
            (agent_id, now, limit),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE campaign_targets SET status='dialing' WHERE id=?",
                [(r["id"],) for r in rows],
            )
            conn.commit()
    return [dict(r) for r in rows]


def set_target_call(target_id: str, call_id: str) -> None:
    with _lock:
        connect().execute(
            "UPDATE campaign_targets SET last_call_id=?, attempts=attempts+1 WHERE id=?",
            (call_id, target_id),
        )
        connect().commit()


def finish_target(target_id: str, status: str, outcome: str | None = None) -> None:
    with _lock:
        connect().execute(
            "UPDATE campaign_targets SET status=?, outcome=? WHERE id=?",
            (status, outcome, target_id),
        )
        connect().commit()


def reschedule_target(target_id: str, next_attempt_at: str) -> None:
    with _lock:
        connect().execute(
            "UPDATE campaign_targets SET status='pending', next_attempt_at=? WHERE id=?",
            (next_attempt_at, target_id),
        )
        connect().commit()


def target_by_call(call_id: str) -> dict[str, Any] | None:
    row = connect().execute(
        "SELECT t.*, cam.max_attempts, cam.retry_after_minutes "
        "FROM campaign_targets t JOIN campaigns cam ON cam.id = t.campaign_id "
        "WHERE t.last_call_id=?",
        (call_id,),
    ).fetchone()
    return dict(row) if row else None


def campaign_stats(campaign_id: str) -> dict[str, int]:
    rows = connect().execute(
        "SELECT status, count(*) AS n FROM campaign_targets "
        "WHERE campaign_id=? GROUP BY status",
        (campaign_id,),
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def retarget_pending(campaign_id: str, agent_id: str, list_id: str) -> int:
    """Realign the queue after a campaign's agent or contact list was changed.

    Only `pending` rows move: a target already `dialing` is mid-conversation
    under the old agent and has to settle there. Contacts dropped from the
    queue are cancelled rather than deleted so they still show in the stats.
    """
    with _lock:
        conn = connect()
        conn.execute(
            "UPDATE campaign_targets SET status='cancelled' "
            "WHERE campaign_id=? AND status='pending' AND contact_id NOT IN "
            "      (SELECT id FROM contacts WHERE list_id=?)",
            (campaign_id, list_id),
        )
        cursor = conn.execute(
            "UPDATE campaign_targets SET agent_id=? "
            "WHERE campaign_id=? AND status='pending' AND agent_id<>?",
            (agent_id, campaign_id, agent_id),
        )
        conn.commit()
    return cursor.rowcount


def cancel_pending_targets(campaign_id: str) -> int:
    with _lock:
        cursor = connect().execute(
            "UPDATE campaign_targets SET status='cancelled' "
            "WHERE campaign_id=? AND status='pending'",
            (campaign_id,),
        )
        connect().commit()
    return cursor.rowcount


def count_live_calls(agent_id: str | None = None) -> int:
    """Calls currently occupying a concurrency slot.

    Bounded by `created_at` because a Twilio status webhook that never arrives
    would otherwise leave a row in `dialing` forever, and every such row would
    permanently shrink the number of calls the agent is allowed to make.
    """
    cutoff = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    sql = (
        "SELECT count(*) AS n FROM calls "
        "WHERE status IN ('queued','dialing','initiated','ringing','in-progress') "
        "AND created_at > ?"
    )
    params: list[Any] = [cutoff]
    if agent_id:
        sql += " AND agent_id=?"
        params.append(agent_id)
    return int(connect().execute(sql, params).fetchone()["n"])


# ---------------------------------------------------------------------------
# Suppressions — do not call
# ---------------------------------------------------------------------------
def is_suppressed(phone_e164: str, agent_id: str, org_id: str = DEFAULT_ORG) -> bool:
    """Checked at dial time, not import time: someone who asks not to be called
    while a campaign is already running must not be called again."""
    row = connect().execute(
        "SELECT 1 FROM suppressions WHERE org_id=? AND phone_e164=? "
        "AND (agent_id IS NULL OR agent_id='' OR agent_id=?) LIMIT 1",
        (org_id, phone_e164, agent_id),
    ).fetchone()
    return row is not None


def add_suppression(
    phone_e164: str, agent_id: str = "", reason: str = "", org_id: str = DEFAULT_ORG
) -> str:
    suppression_id = uuid.uuid4().hex[:12]
    with _lock:
        connect().execute(
            "INSERT INTO suppressions(id, org_id, phone_e164, agent_id, reason, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (suppression_id, org_id, phone_e164, agent_id or None, reason, _now()),
        )
        connect().commit()
    return suppression_id


def list_suppressions(org_id: str = DEFAULT_ORG) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM suppressions WHERE org_id=? ORDER BY created_at DESC",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_suppression(suppression_id: str) -> None:
    with _lock:
        connect().execute("DELETE FROM suppressions WHERE id=?", (suppression_id,))
        connect().commit()


# ---------------------------------------------------------------------------
# Worker registry
# ---------------------------------------------------------------------------
def get_config_version(agent_id: str) -> int:
    """What a running worker compares its loaded config against."""
    row = connect().execute(
        "SELECT config_version FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    return int(row["config_version"]) if row else 0


def upsert_worker(
    agent_id: str, pid: int, port: int, status: str, config_version: int
) -> None:
    now = _now()
    with _lock:
        connect().execute(
            "INSERT INTO workers"
            "(agent_id, pid, port, status, config_version, started_at, last_heartbeat) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET pid=excluded.pid, port=excluded.port, "
            "status=excluded.status, config_version=excluded.config_version, "
            "started_at=excluded.started_at, last_heartbeat=excluded.last_heartbeat",
            (agent_id, pid, port, status, config_version, now, now),
        )
        connect().commit()


def set_worker_status(agent_id: str, status: str) -> None:
    with _lock:
        connect().execute(
            "UPDATE workers SET status=?, last_heartbeat=? WHERE agent_id=?",
            (status, _now(), agent_id),
        )
        connect().commit()


def get_worker(agent_id: str) -> dict[str, Any] | None:
    row = connect().execute(
        "SELECT * FROM workers WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return dict(row) if row else None


def list_workers() -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT w.*, a.name AS agent_name FROM workers w "
        "LEFT JOIN agents a ON a.id = w.agent_id ORDER BY w.started_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_worker(agent_id: str) -> None:
    with _lock:
        connect().execute("DELETE FROM workers WHERE agent_id=?", (agent_id,))
        connect().commit()
