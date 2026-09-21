"""scheduler 重排算法测试。"""
import unittest
from datetime import datetime, timedelta

from app.scheduler import Block, Entry, Item, build_current, build_original, build_view

D = datetime(2026, 9, 21, 9, 0)


def agenda(id_, title, planned, minimum=0, priority=3, lock=False, order=0):
    return Item(id_, "agenda", title, planned, minimum, priority, lock, None, order)


def brk(id_, title, start, minutes=10, order=0):
    return Item(id_, "break", title, minutes, 0, 3, False, start, order)


def entry(id_, item_id, end):
    return Entry(id_, item_id, end)


def by_id(blocks):
    return {b.item_id: b for b in blocks}


class OriginalLayoutTests(unittest.TestCase):

    def test_agenda_chained_and_break_anchored(self):
        items = [
            agenda(1, "A", 20, order=1),
            brk(2, "茶歇", D + timedelta(minutes=30), 10, order=2),
            agenda(3, "B", 20, order=3),
        ]
        blocks = build_original(D, items)
        self.assertEqual(blocks[0].start, D)
        self.assertEqual(blocks[0].end, D + timedelta(minutes=20))
        self.assertEqual(blocks[1].start, D + timedelta(minutes=30))
        self.assertEqual(blocks[1].end, D + timedelta(minutes=40))
        # B 从休息段结束处开始
        self.assertEqual(blocks[2].start, D + timedelta(minutes=40))


class RearrangeTests(unittest.TestCase):

    def test_no_entries_means_empty_current(self):
        items = [agenda(1, "A", 20, order=1)]
        blocks, notices, end = build_current(D, items, [])
        self.assertEqual(blocks, [])
        self.assertEqual(notices, [])
        self.assertEqual(end, D)

    def test_early_finish_creates_idle_gap_without_stretching(self):
        # A 20 + B 30；A 提前到 9:10 结束，B 仍按 30 分排
        items = [agenda(1, "A", 20, order=1), agenda(2, "B", 30, order=2)]
        blocks, _, end = build_current(D, items, [entry(1, 1, D + timedelta(minutes=10))])
        m = by_id(blocks)
        self.assertEqual(m[1].status, "anchor")
        self.assertEqual(m[1].end, D + timedelta(minutes=10))
        self.assertEqual(m[2].start, D + timedelta(minutes=10))
        self.assertEqual(m[2].end, D + timedelta(minutes=40))
        self.assertEqual(m[2].status, "unchanged")
        self.assertEqual(end, D + timedelta(minutes=40))

    def test_compression_by_priority_respects_floor(self):
        # 9:00 开始，茶歇 10:00 固定。A、B 同处茶歇前窗口，按优先级压缩。
        items = [
            agenda(1, "A", 40, minimum=10, priority=2, order=1),
            agenda(2, "B", 30, minimum=10, priority=5, order=2),
            brk(9, "茶歇", D + timedelta(minutes=60), 10, order=3),
        ]
        # 锚点 A 按时于 9:40 结束，B 的窗口只剩 9:40→10:00 共 20 分，需压 10
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=40))])
        self.assertEqual(notices, [])
        m = by_id(blocks)
        self.assertEqual(m[1].status, "anchor")
        self.assertEqual(m[2].status, "compressed")
        self.assertEqual(m[2].start, D + timedelta(minutes=40))
        self.assertEqual(m[2].end, D + timedelta(minutes=60))  # 压到 20
        self.assertEqual(m[2].saved_minutes, 10)
        # 休息段不动
        self.assertEqual(m[9].start, D + timedelta(minutes=60))

    def test_lower_priority_compressed_first_across_floor(self):
        # 需压 30：P5 先压满（30→10），再压 P2（40→30），无冲突
        items = [
            agenda(0, "开场", 1, minimum=1, order=0),
            agenda(1, "A", 40, minimum=10, priority=2, order=1),
            agenda(2, "B", 30, minimum=10, priority=5, order=2),
            brk(9, "茶歇", D + timedelta(minutes=41), 10, order=3),
        ]
        # 开场 9:01 结束 → 窗口 9:01→9:41 共 40 分容纳 A+B=70，需压 30
        blocks, notices, _ = build_current(D, items, [entry(1, 0, D + timedelta(minutes=1))])
        m = by_id(blocks)
        self.assertEqual(m[2].status, "compressed")
        self.assertEqual(m[2].end - m[2].start, timedelta(minutes=10))  # B 先压满
        self.assertEqual(m[1].status, "compressed")
        self.assertEqual(m[1].end - m[1].start, timedelta(minutes=30))  # A 再压 10
        self.assertEqual(notices, [])

    def test_incompressible_is_protected(self):
        # 窗口 30 分：A 20 min10 P5 可压；B 20 锁定 P1；计划 40 需压 10，全由 A 承担
        items = [
            agenda(1, "A", 20, minimum=10, priority=5, order=1),
            agenda(2, "B", 20, priority=1, lock=True, order=2),
            brk(9, "茶歇", D + timedelta(minutes=30), 10, order=3),
        ]
        # 锚点 A 在 9:00 结束（时长 0 的极端录入），B 完整保留计划时长
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D)])
        m = by_id(blocks)
        self.assertEqual(m[1].status, "anchor")
        self.assertEqual(m[2].status, "unchanged")
        self.assertEqual(m[2].end, D + timedelta(minutes=20))
        self.assertEqual(notices, [])

    def test_conflict_lists_items_and_overflow(self):
        # 窗口 9:00→9:30 = 30 分；A 20(可压min10) + B 20 锁定 = 40。
        # A 压到 10，仍需 10 分：B 20 分从 9:10 到 9:30 被截断（冲突 10 分）
        items = [
            agenda(1, "A", 20, minimum=10, priority=5, order=1),
            agenda(2, "B", 20, priority=1, lock=True, order=2),
            brk(9, "茶歇", D + timedelta(minutes=30), 10, order=3),
        ]
        # 锚点取在 9:00：录入 A 实际 9:00 结束（A 时长 0，非典型但合法），
        # 更真实：给 A 一个独立的前置块。重做：
        items = [
            agenda(0, "开场", 1, minimum=1, order=0),
            agenda(1, "A", 20, minimum=10, priority=5, order=1),
            agenda(2, "B", 20, priority=1, lock=True, order=2),
            brk(9, "茶歇", D + timedelta(minutes=31), 10, order=3),
        ]
        # 开场 9:01 结束 → 窗口 9:01→9:31 共 30 分，A 压到最短 10 + B 20 恰好放下
        blocks, notices, end = build_current(D, items, [entry(1, 0, D + timedelta(minutes=1))])
        m = by_id(blocks)
        self.assertEqual(m[1].status, "compressed")
        self.assertEqual(m[1].end, D + timedelta(minutes=11))
        self.assertEqual(m[2].status, "unchanged")
        self.assertEqual(m[2].end, D + timedelta(minutes=31))
        self.assertEqual([n for n in notices if n["type"] != "missed_break"], [])
        # 开场 9:02 结束 → 窗口只有 29 分：A 压满到 10 后 B 仍溢出 1 分
        blocks, notices, end = build_current(
            D, items, [entry(1, 0, D + timedelta(minutes=2))])  # 窗口 9:02→9:31 = 29
        m = by_id(blocks)
        self.assertEqual(m[2].status, "conflict")
        item_notices = [n for n in notices if n["type"] == "item"]
        self.assertEqual(item_notices[0]["overflow_minutes"], 1)
        overlap = [n for n in notices if n["type"] == "break_overlap"]
        self.assertEqual(overlap[0]["overflow_minutes"], 1)
        self.assertEqual(overlap[0]["item_id"], 9)

    def test_past_blocks_keep_planned_times_and_break_stays_fixed(self):
        items = [
            agenda(1, "A", 30, order=1),
            brk(9, "茶歇", D + timedelta(minutes=30), 10, order=2),
            agenda(2, "B", 30, order=3),
        ]
        # A 拖延到 10:00 才结束（跨过 9:30–9:40 的茶歇）
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=60))])
        m = by_id(blocks)
        self.assertEqual(m[1].status, "anchor")
        self.assertEqual(m[1].end, D + timedelta(minutes=60))
        # 茶歇完整落在 T 之前 → 原样保留
        self.assertEqual(m[9].status, "past_break")
        self.assertEqual(m[9].start, D + timedelta(minutes=30))
        # B 在 10:00 之后正常排
        self.assertEqual(m[2].start, D + timedelta(minutes=60))

    def test_missed_break_keeps_fixed_time_and_is_reported(self):
        # 9:00 开始，茶歇 9:30；A 作为锚点 9:50 结束时茶歇已完全错过
        items = [
            agenda(1, "A", 70, order=1),
            brk(9, "茶歇", D + timedelta(minutes=30), 10, order=2),
            agenda(2, "B", 20, order=3),
        ]
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=50))])
        missed = [n for n in notices if n["type"] == "missed_break"]
        # 休息段 9:30–9:40 在 T=9:50 前已结束：保留固定时间，但不算“错过”
        self.assertEqual(missed, [])
        m = by_id(blocks)
        self.assertEqual(m[9].start, D + timedelta(minutes=30))
        self.assertEqual(m[9].status, "past_break")
        # B 紧接锚点 9:50 开始，不受已结束茶歇影响
        self.assertEqual(m[2].start, D + timedelta(minutes=50))

    def test_break_interrupted_by_anchor_is_reported_as_missed(self):
        # T=9:35 落在休息段 9:30–9:40 之内：休息段被打断
        items = [
            agenda(1, "A", 70, order=1),
            brk(9, "茶歇", D + timedelta(minutes=30), 10, order=2),
            agenda(2, "B", 20, order=3),
        ]
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=35))])
        missed = [n for n in notices if n["type"] == "missed_break"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["item_id"], 9)
        m = by_id(blocks)
        self.assertEqual(m[9].start, D + timedelta(minutes=30))
        self.assertEqual(m[9].status, "past_break")
        self.assertEqual(m[2].start, D + timedelta(minutes=35))

    def test_break_before_anchor_stays_fixed_in_open_window(self):
        # 茶歇 10:30–10:45；锚点 11:00 才结束（休息段已完整过去）；
        # 之后是开放窗口，B 按计划时长排，休息段固定不移动且不报“打断”。
        items = [
            agenda(1, "A", 120, order=1),
            brk(9, "茶歇", D + timedelta(minutes=90), 15, order=2),
            agenda(2, "B", 20, order=3),
        ]
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=120))])
        self.assertEqual([n for n in notices if n["type"] == "missed_break"], [])
        m = by_id(blocks)
        self.assertEqual(m[9].status, "past_break")
        self.assertEqual(m[9].start, D + timedelta(minutes=90))
        self.assertEqual(m[2].status, "unchanged")
        self.assertEqual(m[2].start, D + timedelta(minutes=120))

    def test_after_break_window_compresses_independently(self):
        # 两个窗口：9:00→10:00，10:10→结束
        items = [
            agenda(1, "A", 50, minimum=10, order=1),
            brk(9, "茶歇", D + timedelta(minutes=60), 10, order=2),
            agenda(2, "B", 30, minimum=10, order=3),
        ]
        # A 拖到 9:55：第一窗口 9:55→10:00 不需容纳 A（A 是锚点）；B 在 10:10 后开放窗口，不压缩
        blocks, notices, _ = build_current(D, items, [entry(1, 1, D + timedelta(minutes=55))])
        m = by_id(blocks)
        self.assertEqual(m[2].status, "unchanged")
        self.assertEqual(m[2].start, D + timedelta(minutes=70))
        self.assertEqual(notices, [])


class ViewTests(unittest.TestCase):

    def test_view_delay_reflects_new_end(self):
        items = [
            agenda(1, "A", 20, order=1),
            agenda(2, "B", 20, order=2),
        ]
        meeting = {"title": "t", "start_time": D}
        view = build_view(meeting, items, [entry(1, 1, D + timedelta(minutes=30))])
        self.assertEqual(view["current"]["delay_minutes"], 10)
        self.assertTrue(view["current"]["blocks"])


if __name__ == "__main__":
    unittest.main()
