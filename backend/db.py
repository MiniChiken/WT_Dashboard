import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional


def _default_db_path() -> Path:
    # Frozen (PyInstaller) builds run from a temp extraction dir that's wiped
    # and recreated on every launch - a DB living there would lose all data
    # between runs. Use a stable per-user location instead, same as any
    # normal installed Windows app; the source/dev case is unchanged (DB
    # stays right next to this file, as it always has).
    if getattr(sys, "frozen", False):
        base = Path(os.getenv("LOCALAPPDATA", Path.home())) / "WTDashboard"
        base.mkdir(parents=True, exist_ok=True)
        return base / "stats.db"
    return Path(__file__).parent / "stats.db"


DB_PATH = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL,
    army TEXT,
    vehicle_type TEXT,
    map_image BLOB,
    map_image_type TEXT,
    my_name TEXT,
    provisional_my_name TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    match_time REAL,
    kind TEXT NOT NULL,
    msg TEXT,
    is_enemy INTEGER,
    verb TEXT,
    actor_name TEXT,
    actor_vehicle TEXT,
    target_name TEXT,
    target_vehicle TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    state_json TEXT,
    indicators_json TEXT,
    map_obj_json TEXT,
    mission_json TEXT,
    map_info_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry_samples(session_id);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "map_image" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN map_image BLOB")
    if "map_image_type" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN map_image_type TEXT")
    if "my_name" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN my_name TEXT")
    if "provisional_my_name" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN provisional_my_name TEXT")

    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for col in ("verb", "actor_name", "actor_vehicle", "target_name", "target_vehicle"):
        if col not in event_cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def start_session(army: Optional[str], vehicle_type: Optional[str], map_image: bytes = None, map_image_type: str = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO sessions (start_ts, army, vehicle_type, map_image, map_image_type) VALUES (?, ?, ?, ?, ?)",
        (time.time(), army, vehicle_type, map_image, map_image_type),
    )
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return session_id


def get_session_map_image(session_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT map_image, map_image_type FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    conn.close()
    if not row or row["map_image"] is None:
        return None, None
    return row["map_image"], row["map_image_type"]


def end_session(session_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET end_ts = ? WHERE id = ?", (time.time(), session_id)
    )
    conn.commit()
    conn.close()


def log_event(session_id: int, kind: str, raw: dict, msg: str = None, match_time: float = None,
              is_enemy: bool = None, verb: str = None, actor_name: str = None,
              actor_vehicle: str = None, target_name: str = None, target_vehicle: str = None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO events
           (session_id, ts, match_time, kind, msg, is_enemy, verb, actor_name, actor_vehicle, target_name, target_vehicle, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, time.time(), match_time, kind, msg, None if is_enemy is None else int(is_enemy),
         verb, actor_name, actor_vehicle, target_name, target_vehicle, json.dumps(raw)),
    )
    conn.commit()
    conn.close()


def set_session_my_name(session_id: int, name: str):
    conn = get_conn()
    conn.execute("UPDATE sessions SET my_name = ? WHERE id = ?", (name, session_id))
    conn.commit()
    conn.close()


def get_session_my_name(session_id: int):
    conn = get_conn()
    row = conn.execute("SELECT my_name FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row["my_name"] if row else None


def set_session_provisional_my_name(session_id: int, name: str):
    conn = get_conn()
    conn.execute("UPDATE sessions SET provisional_my_name = ? WHERE id = ?", (name, session_id))
    conn.commit()
    conn.close()


def get_session_provisional_my_name(session_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT provisional_my_name FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return row["provisional_my_name"] if row else None


def log_telemetry_sample(session_id: int, state: dict, indicators: dict, map_obj: list, mission: dict, map_info: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO telemetry_samples
           (session_id, ts, state_json, indicators_json, map_obj_json, mission_json, map_info_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, time.time(), json.dumps(state), json.dumps(indicators),
         json.dumps(map_obj), json.dumps(mission), json.dumps(map_info)),
    )
    conn.commit()
    conn.close()


def get_session_telemetry(session_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM telemetry_samples WHERE session_id = ? ORDER BY id ASC", (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


SESSION_COLUMNS = """
    id, start_ts, end_ts, army, vehicle_type, my_name, provisional_my_name,
    (map_image IS NOT NULL) AS has_map_image
"""


def list_sessions(limit: int = 50):
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session(session_id: int):
    conn = get_conn()
    session = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    events = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY id ASC", (session_id,)
    ).fetchall()
    conn.close()
    if not session:
        return None
    return {"session": dict(session), "events": [dict(e) for e in events]}


def get_session_start_ts(session_id: int):
    conn = get_conn()
    row = conn.execute("SELECT start_ts FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row["start_ts"] if row else None


def get_known_vehicle_armies():
    """Every (vehicle_type, army) pair the player has ever flown/driven, across
    all telemetry ever recorded. Used as a growing, evidence-based lookup for
    classifying an enemy's vehicle by army when a kill-feed message names a
    vehicle the player has personally used before - only covers vehicles
    that have shown up as *our own* vehicle at some point, so coverage grows
    with play but is never complete for vehicles never used locally.

    Reads telemetry_samples (every polled indicators.type/army seen) rather
    than sessions.vehicle_type/army (only the vehicle a session *started*
    in) - combined-arms matches let a player switch between ground and air
    mid-session, and a plane only ever reached via such a switch was
    invisible to the old sessions-only lookup, meaning that plane's kills
    against the player could never be recognized as an air threat."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT
               json_extract(indicators_json, '$.type') AS vehicle_type,
               json_extract(indicators_json, '$.army') AS army
           FROM telemetry_samples
           WHERE json_extract(indicators_json, '$.type') IS NOT NULL
             AND json_extract(indicators_json, '$.army') IS NOT NULL"""
    ).fetchall()
    conn.close()
    return {row["vehicle_type"]: row["army"] for row in rows}


def get_session_export(session_id: int):
    result = get_session(session_id)
    if result is None:
        return None
    result["telemetry"] = get_session_telemetry(session_id)
    return result
