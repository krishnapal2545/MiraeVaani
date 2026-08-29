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
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.models import AgentConfig

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    provider    TEXT PRIMARY KEY,
    secret_enc  BLOB NOT NULL,
    updated_at  TEXT NOT NULL
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
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    for column, sql_type, default in _AGENT_MIGRATIONS:
        if column in existing:
            continue
        literal = f"'{default}'" if isinstance(default, str) else str(default)
        conn.execute(
            f"ALTER TABLE agents ADD COLUMN {column} {sql_type} "
            f"NOT NULL DEFAULT {literal}"
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(get_settings().DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def init() -> None:
    connect()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def set_credential(provider: str, secret: str) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO credentials(provider, secret_enc, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET secret_enc=excluded.secret_enc, "
            "updated_at=excluded.updated_at",
            (provider, encrypt(secret), _now()),
        )
        connect().commit()


def get_credential(provider: str) -> str:
    row = connect().execute(
        "SELECT secret_enc FROM credentials WHERE provider=?", (provider,)
    ).fetchone()
    return decrypt(row["secret_enc"]) if row else ""


def get_credential_json(provider: str) -> dict[str, Any]:
    """For providers whose credential is a JSON blob (twilio: sid/token/number)."""
    raw = get_credential(provider)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def set_credential_json(provider: str, data: dict[str, Any]) -> None:
    set_credential(provider, json.dumps(data))


def list_credential_providers() -> list[str]:
    return [r["provider"] for r in connect().execute("SELECT provider FROM credentials")]


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
        connect().execute(
            f"UPDATE agents SET {assignments}, updated_at=? WHERE id=?",
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
