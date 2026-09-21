"""会议议程实时重排 —— FastAPI 应用入口。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .scheduler import build_view, next_recordable_item

app = FastAPI(title="会议议程实时重排")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------- 请求模型 ----------

class MeetingReset(BaseModel):
    title: str = Field(default="团队例会", max_length=100)
    start_time: str = Field(description="会议开始时间，ISO 格式，如 2026-09-21T09:00")


class ItemCreate(BaseModel):
    kind: str = Field(pattern="^(agenda|break)$")
    title: str = Field(min_length=1, max_length=100)
    planned_minutes: float = Field(gt=0, le=1440)
    min_minutes: float = Field(default=0, ge=0, le=1440)
    priority: int = Field(default=3, ge=1, le=5)
    incompressible: bool = False
    fixed_start: Optional[str] = Field(
        default=None, description="休息段固定开始时间（休息段必填），ISO 格式")


class EntryCreate(BaseModel):
    item_id: int
    actual_end: str = Field(description="该项实际结束时间，ISO 格式")


# ---------- 状态组装 ----------

def state() -> dict:
    meeting = db.get_meeting()
    items = db.load_items()
    entries = db.load_entries()
    view = build_view(meeting, items, entries)
    return {
        "meeting": {
            "title": meeting["title"],
            "start_time": meeting["start_time"].isoformat(timespec="minutes"),
        },
        "items": [
            {
                "id": it.id, "kind": it.kind, "title": it.title,
                "planned_minutes": it.planned_minutes,
                "min_minutes": it.min_minutes, "priority": it.priority,
                "incompressible": it.incompressible,
                "fixed_start": it.fixed_start.isoformat(timespec="minutes")
                if it.fixed_start else None,
                "sort_order": it.sort_order,
            } for it in items
        ],
        "entries": [
            {"id": e.id, "item_id": e.item_id,
             "actual_end": e.actual_end.isoformat(timespec="minutes")}
            for e in entries
        ],
        "next_recordable_id": next_recordable_item(items, entries),
        **view,
    }


def _parse_dt(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} 时间格式无效，应为 YYYY-MM-DDTHH:MM")
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


# ---------- 路由 ----------

@app.get("/api/state")
def get_state():
    return state()


@app.post("/api/meeting/reset", status_code=200)
def reset_meeting(body: MeetingReset):
    start = _parse_dt(body.start_time, "start_time")
    db.reset_meeting(body.title.strip() or "团队例会", start)
    return state()


@app.post("/api/items", status_code=201)
def create_item(body: ItemCreate):
    if body.min_minutes > body.planned_minutes:
        raise HTTPException(422, "最短时长不能大于计划时长")
    fixed_start = None
    if body.kind == "break":
        if not body.fixed_start:
            raise HTTPException(422, "休息段必须填写固定开始时间")
        fixed_start = _parse_dt(body.fixed_start, "fixed_start")
    db.insert_item(body.kind, body.title.strip(), body.planned_minutes,
                   body.min_minutes, body.priority, body.incompressible, fixed_start)
    return state()


@app.post("/api/items/{item_id}/move")
def move_item(item_id: int, direction: str = Query(pattern="^(up|down)$")):
    if direction not in ("up", "down"):
        raise HTTPException(422, "direction 只能是 up 或 down")
    try:
        db.move_item(item_id, direction)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return state()


@app.delete("/api/items/{item_id}")
def remove_item(item_id: int):
    db.delete_item(item_id)
    return state()


@app.post("/api/entries", status_code=201)
def add_entry(body: EntryCreate):
    items = db.load_items()
    target = next((it for it in items if it.id == body.item_id), None)
    if target is None or target.kind != "agenda":
        raise HTTPException(404, "议程项不存在或为休息段")
    actual_end = _parse_dt(body.actual_end, "actual_end")
    if actual_end < db.get_meeting()["start_time"]:
        raise HTTPException(422, "实际结束时间不能早于会议开始时间")
    entries = db.load_entries()
    if entries and actual_end < max(e.actual_end for e in entries):
        raise HTTPException(422, "实际结束时间不能早于上一次录入的时间")
    expected = next_recordable_item(items, entries)
    if expected != body.item_id:
        raise HTTPException(409, "请按议程顺序录入：请先录入上一项的实际结束时间")
    db.insert_entry(body.item_id, actual_end)
    return state()


@app.post("/api/entries/undo")
def undo_entry():
    if not db.undo_last_entry():
        raise HTTPException(409, "没有可撤销的实况录入")
    return state()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
