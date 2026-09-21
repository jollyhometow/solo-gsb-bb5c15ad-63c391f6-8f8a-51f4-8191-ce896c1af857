"""SQLite 持久层：单会议模型、议程项、实况录入。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .scheduler import Entry, Item

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agenda.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        init_db(_conn)
    return _conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meeting (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT NOT NULL,
            start_time TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('agenda','break')),
            title TEXT NOT NULL,
            planned_minutes REAL NOT NULL,
            min_minutes REAL NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 3,
            incompressible INTEGER NOT NULL DEFAULT 0,
            fixed_start TEXT,
            sort_order INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
            actual_end TEXT NOT NULL
        );
        """
    )
    conn.commit()
    _seed_if_empty(conn)


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    """首次启动时播种一份演示会议（重置后不再播种）。"""
    if conn.execute("SELECT 1 FROM meeting WHERE id = 1").fetchone():
        return
    start = datetime(2026, 9, 21, 9, 0)
    conn.execute("INSERT INTO meeting (id, title, start_time) VALUES (1, ?, ?)",
                 ("产品迭代评审会", start.isoformat()))
    demo = [
        ("agenda", "开场与目标同步", 10, 5, 1, 0, None),
        ("agenda", "上轮数据复盘", 20, 10, 3, 0, None),
        ("agenda", "需求优先级评审", 40, 20, 1, 0, None),
        ("break", "茶歇", 15, 0, 3, 0, datetime(2026, 9, 21, 10, 30)),
        ("agenda", "技术方案讨论", 30, 10, 2, 0, None),
        ("agenda", "风险与排期确认", 20, 5, 4, 0, None),
        ("agenda", "行动项总结", 10, 5, 1, 1, None),
    ]
    for order, (kind, title, planned, minimum, prio, lock, fs) in enumerate(demo, start=1):
        conn.execute(
            """INSERT INTO item (kind, title, planned_minutes, min_minutes, priority,
                                 incompressible, fixed_start, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, title, planned, minimum, prio, lock, fs.isoformat() if fs else None, order),
        )
    conn.commit()


# ---------- 数据装载 ----------

def get_meeting() -> dict:
    row = get_conn().execute("SELECT * FROM meeting WHERE id = 1").fetchone()
    return {"title": row["title"], "start_time": datetime.fromisoformat(row["start_time"])}


def load_items() -> list[Item]:
    rows = get_conn().execute("SELECT * FROM item ORDER BY sort_order, id").fetchall()
    return [Item(
        id=r["id"], kind=r["kind"], title=r["title"],
        planned_minutes=float(r["planned_minutes"]),
        min_minutes=float(r["min_minutes"]), priority=r["priority"],
        incompressible=bool(r["incompressible"]),
        fixed_start=datetime.fromisoformat(r["fixed_start"]) if r["fixed_start"] else None,
        sort_order=r["sort_order"],
    ) for r in rows]


def load_entries() -> list[Entry]:
    rows = get_conn().execute("SELECT * FROM entry ORDER BY id").fetchall()
    return [Entry(id=r["id"], item_id=r["item_id"],
                  actual_end=datetime.fromisoformat(r["actual_end"])) for r in rows]


# ---------- 写操作 ----------

def reset_meeting(title: str, start_time: datetime) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM entry")
        conn.execute("DELETE FROM item")
        conn.execute("DELETE FROM meeting")
        conn.execute("INSERT INTO meeting (id, title, start_time) VALUES (1, ?, ?)",
                     (title, start_time.isoformat()))
        conn.commit()


def insert_item(kind: str, title: str, planned: float, minimum: float,
                priority: int, incompressible: bool,
                fixed_start: Optional[datetime]) -> int:
    with _lock:
        conn = get_conn()
        order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS o FROM item").fetchone()["o"]
        cur = conn.execute(
            """INSERT INTO item (kind, title, planned_minutes, min_minutes, priority,
                                 incompressible, fixed_start, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, title, planned, minimum, priority, int(incompressible),
             fixed_start.isoformat() if fixed_start else None, order),
        )
        conn.commit()
        return cur.lastrowid


def move_item(item_id: int, direction: str) -> None:
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT id, sort_order FROM item ORDER BY sort_order, id").fetchall()
        ids = [r["id"] for r in rows]
        if item_id not in ids:
            raise ValueError("议程项不存在")
        pos = ids.index(item_id)
        swap = pos - 1 if direction == "up" else pos + 1
        if not (0 <= swap < len(ids)):
            return
        other = rows[swap]["id"]
        conn.execute("UPDATE item SET sort_order = ? WHERE id = ?",
                     (rows[swap]["sort_order"], item_id))
        conn.execute("UPDATE item SET sort_order = ? WHERE id = ?",
                     (rows[pos]["sort_order"], other))
        conn.commit()


def delete_item(item_id: int) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM item WHERE id = ?", (item_id,))
        conn.commit()


def insert_entry(item_id: int, actual_end: datetime) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO entry (item_id, actual_end) VALUES (?, ?)",
            (item_id, actual_end.isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def undo_last_entry() -> bool:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT id FROM entry ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM entry WHERE id = ?", (row["id"],))
        conn.commit()
        return True
