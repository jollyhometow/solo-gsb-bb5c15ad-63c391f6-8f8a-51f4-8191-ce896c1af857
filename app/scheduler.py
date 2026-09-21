"""会议议程实时重排核心算法（纯标准库，无框架依赖）。

时间一律使用 naive datetime（本地墙钟时间），时长单位为分钟（float）。

重排规则
--------
1. 以最近一次实况录入的“实际结束时间 T”为锚点：严格结束于 T 之前的块保持
   原计划时间显示为“已结束”；锚点议程块结束于 T。
2. T 之后的议程被固定休息段切分成若干窗口。固定休息段绝不移动。
3. 窗口足够时，议程按计划时长排列；窗口不足时，按优先级压缩可压缩议程
   （priority 数字越大级别越低、越先被压缩；同优先级时靠后的议程先压缩），
   且不低于各自的最短时长；标记为“不可压缩”的议程不动。
4. 若所有可压缩议程都压到最短仍放不下，则进入冲突状态：列出无法完整容纳的
   议程项及其超出分钟数，并标出与哪个固定休息段重叠及总溢出分钟。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

EPS = 1e-6


@dataclass
class Item:
    id: int
    kind: str  # 'agenda' | 'break'
    title: str
    planned_minutes: float
    min_minutes: float = 0.0
    priority: int = 3
    incompressible: bool = False
    fixed_start: Optional[datetime] = None
    sort_order: int = 0


@dataclass
class Entry:
    id: int
    item_id: int
    actual_end: datetime


@dataclass
class Block:
    item_id: int
    kind: str
    title: str
    start: datetime
    end: datetime
    planned_minutes: float
    status: str  # past / anchor / unchanged / compressed / conflict / break / past_break
    saved_minutes: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "title": self.title,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "start_full": self.start.isoformat(),
            "end_full": self.end.isoformat(),
            "duration_minutes": round((self.end - self.start).total_seconds() / 60, 1),
            "planned_minutes": round(self.planned_minutes, 1),
            "saved_minutes": round(self.saved_minutes, 1),
            "status": self.status,
        }


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def build_original(start: datetime, items: list[Item]) -> list[Block]:
    """按顺序排布原始计划：普通项首尾相接，休息段锚定在 fixed_start。"""
    blocks: list[Block] = []
    cursor = start
    for it in items:
        if it.kind == "break":
            s = it.fixed_start
            blocks.append(
                Block(it.id, "break", it.title, s, s + timedelta(minutes=it.planned_minutes),
                      it.planned_minutes, "break")
            )
            cursor = max(cursor, s + timedelta(minutes=it.planned_minutes))
        else:
            blocks.append(
                Block(it.id, "agenda", it.title, cursor,
                      cursor + timedelta(minutes=it.planned_minutes),
                      it.planned_minutes, "unchanged")
            )
            cursor += timedelta(minutes=it.planned_minutes)
    return blocks


@dataclass
class _Demand:
    idx: int
    item: Item
    planned_demand: float  # 窗口内仍需安排的计划分钟数


def _fit_window(group: list[_Demand], cap_minutes: Optional[float]
                ) -> tuple[dict[int, float], bool, float]:
    """计算一个窗口内各议程的实际安排时长。

    返回 (idx -> 实际时长, 是否冲突, 总溢出分钟)。cap 为 None 表示开放窗口。
    """
    durations = {g.idx: g.planned_demand for g in group}
    planned_total = sum(g.planned_demand for g in group)
    if cap_minutes is None or planned_total <= cap_minutes + EPS:
        return durations, False, 0.0

    need = planned_total - cap_minutes
    # 可压缩量 = 仍需安排时长 − 最短时长。
    # 优先级数字大（级别低）的先压；同优先级时靠后（idx 大）的先压。
    compressible = sorted(
        (g for g in group if not g.item.incompressible
         and g.planned_demand - g.item.min_minutes > EPS),
        key=lambda g: (g.item.priority, g.idx),
        reverse=True,
    )
    for g in compressible:
        cut = min(g.planned_demand - g.item.min_minutes, need)
        durations[g.idx] -= cut
        need -= cut
        if need <= EPS:
            break

    if need <= EPS:
        return durations, False, 0.0

    # 全部压到最短仍不足 —— 冲突。need 即总溢出分钟。
    return durations, True, need


def build_current(start: datetime, items: list[Item], entries: list[Entry]
                  ) -> tuple[list[Block], list[dict[str, Any]], datetime]:
    """依据实况录入重排。返回 (当前时间轴块, 冲突/提示列表, 会议实际结束时间)。"""
    if not entries:
        return [], [], start

    orig = build_original(start, items)
    by_id = {it.id: it for it in items}

    latest = max(entries, key=lambda e: e.id)
    T = latest.actual_end
    recorded_ids = {e.item_id for e in entries}

    blocks: list[Block] = []
    notices: list[dict[str, Any]] = []

    # 1) 顺序扫描直到越过锚点：
    #    - 已录入的普通议程（锚点之前的）是真实已结束的块，原样保留为 past；
    #    - 锚点议程以 T 截断；
    #    - 休息段按真实时间处理：T 之前完整结束→past_break；T 落在段内→打断。
    #    未录入的普通议程一律留给步骤 2 重排。
    i = 0
    n = len(orig)
    cursor = T
    while i < n:
        ob = orig[i]
        if ob.item_id == latest.item_id and ob.kind == "agenda":
            blocks.append(Block(ob.item_id, "agenda", ob.title, ob.start, T,
                                ob.planned_minutes, "anchor",
                                saved_minutes=max(0.0, _minutes(ob.start, ob.end)
                                                  - _minutes(ob.start, T))))
            i += 1
            continue
        if ob.kind == "agenda":
            if ob.item_id in recorded_ids:
                blocks.append(Block(ob.item_id, "agenda", ob.title, ob.start, ob.end,
                                    ob.planned_minutes, "past"))
                i += 1
                continue
            # 未录入议程：停止分段，交给步骤 2 重排
            break
        # 休息段（含锚点之后、起点早于 T 的）
        if ob.end <= T - timedelta(seconds=1):
            blocks.append(Block(ob.item_id, "break", ob.title, ob.start, ob.end,
                                ob.planned_minutes, "past_break"))
            i += 1
            continue
        if ob.start <= T:
            blocks.append(Block(ob.item_id, "break", ob.title, ob.start, ob.end,
                                ob.planned_minutes, "past_break"))
            notices.append({
                "type": "missed_break",
                "item_id": ob.item_id,
                "title": ob.title,
                "fixed_start": ob.start.isoformat(timespec="minutes"),
            })
            i += 1
            continue
        break

    # 2) T 之后按固定休息段切窗，逐窗排布
    while i < n:
        group: list[_Demand] = []
        br: Optional[Item] = None
        win_end: Optional[datetime] = None
        while i < n:
            it = by_id[orig[i].item_id]
            if it.kind == "agenda":
                # 上一项拖延时本项实际尚未开始，仍按完整计划需求参与排布。
                group.append(_Demand(i, it, it.planned_minutes))
                i += 1
            else:
                fs = it.fixed_start
                be = fs + timedelta(minutes=it.planned_minutes)
                if fs <= cursor + timedelta(seconds=1):
                    # 休息段起点已到/已过：固定时间不移动，仍画在固定位置。
                    blocks.append(Block(it.id, "break", it.title, fs, be,
                                        it.planned_minutes, "past_break"))
                    if fs < cursor < be:
                        # 当前时刻落在休息段内：休息段被议程打断（错过）。
                        notices.append({
                            "type": "missed_break",
                            "item_id": it.id,
                            "title": it.title,
                            "fixed_start": fs.isoformat(timespec="minutes"),
                        })
                    i += 1
                    continue
                br = it
                win_end = fs
                i += 1
                break

        cap = None if win_end is None else _minutes(cursor, win_end)
        durations, in_conflict, overflow = _fit_window(group, cap)

        run_cursor = cursor
        for g in group:
            dur = durations[g.idx]
            item = g.item
            b_start, b_end = run_cursor, run_cursor + timedelta(minutes=dur)
            run_cursor = b_end
            if in_conflict and win_end is not None and b_end > win_end + timedelta(seconds=1):
                status = "conflict"
                over = _minutes(win_end, b_end) if b_start < win_end else dur
                notices.append({
                    "type": "item",
                    "item_id": item.id,
                    "title": item.title,
                    "priority": item.priority,
                    "incompressible": bool(item.incompressible),
                    "duration_minutes": round(dur, 1),
                    "window_end": win_end.isoformat(timespec="minutes"),
                    "overflow_minutes": round(over, 1),
                    "start": b_start.isoformat(timespec="minutes"),
                    "end": b_end.isoformat(timespec="minutes"),
                })
            elif dur < g.planned_demand - EPS:
                status = "compressed"
            else:
                status = "unchanged"
            blocks.append(Block(item.id, "agenda", item.title, b_start, b_end,
                                g.planned_demand, status,
                                saved_minutes=max(0.0, g.planned_demand - dur)))

        if br is not None:
            if in_conflict and overflow > EPS:
                notices.append({
                    "type": "break_overlap",
                    "item_id": br.id,
                    "title": br.title,
                    "break_start": br.fixed_start.isoformat(timespec="minutes"),
                    "break_end": (br.fixed_start
                                  + timedelta(minutes=br.planned_minutes)).isoformat(timespec="minutes"),
                    "overflow_minutes": round(overflow, 1),
                })
            blocks.append(Block(br.id, "break", br.title, br.fixed_start,
                                br.fixed_start + timedelta(minutes=br.planned_minutes),
                                br.planned_minutes, "break"))
            cursor = br.fixed_start + timedelta(minutes=br.planned_minutes)
        else:
            cursor = run_cursor

    blocks.sort(key=lambda b: (b.start, 0 if b.kind == "break" else 1))
    end = max((b.end for b in blocks), default=start)
    return blocks, notices, end


def build_view(meeting: dict[str, Any], items: list[Item], entries: list[Entry]) -> dict[str, Any]:
    """组装供 API / 前端使用的完整视图（原计划 + 当前计划）。"""
    start = meeting["start_time"]
    original = build_original(start, items)
    current_blocks, notices, current_end = build_current(start, items, entries)
    orig_end = original[-1].end if original else start

    return {
        "original": {
            "blocks": [b.as_dict() for b in original],
            "end": orig_end.isoformat(timespec="minutes"),
        },
        "current": {
            "blocks": [b.as_dict() for b in current_blocks],
            "end": current_end.isoformat(timespec="minutes"),
            "delay_minutes": round(_minutes(orig_end, current_end), 1),
            "conflicts": notices,
        },
    }


def next_recordable_item(items: list[Item], entries: list[Entry]) -> Optional[int]:
    """返回当前允许录入实际结束时间的议程项 id（按顺序的下一个未录入议程）。"""
    agenda = sorted((it for it in items if it.kind == "agenda"),
                    key=lambda it: it.sort_order)
    if not entries:
        return agenda[0].id if agenda else None
    latest = max(entries, key=lambda e: e.id)
    later = [it for it in agenda if it.sort_order > _order_of(items, latest.item_id)]
    return later[0].id if later else None


def _order_of(items: list[Item], item_id: int) -> int:
    for it in items:
        if it.id == item_id:
            return it.sort_order
    return -1
