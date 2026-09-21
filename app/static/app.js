/* 会议议程实时重排 —— 前端逻辑（原生 JS，无框架） */
"use strict";

let S = null;
let pxPerMin = 2.5;
const ROW_H = 64, AXIS_H = 26, LABEL_W = 88;

const $ = (id) => document.getElementById(id);

// ---------- 工具 ----------

function api(path, options = {}) {
  return fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `请求失败 (${r.status})`);
    return data;
  });
}

function pad(n) { return String(n).padStart(2, "0"); }

function toLocalInput(iso) {
  // ISO（naive）-> datetime-local 值
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function hm(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function minutesOf(isoA, isoB) {
  return (new Date(isoA) - new Date(isoB)) / 60000;
}

function localIso(d) {
  // 用本地时区组件拼 naive ISO，避免 toISOString 的 UTC 偏移
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ---------- 渲染 ----------

function render() {
  renderMeta();
  renderItemList();
  renderTimelines();
  renderBanners();
}

function renderMeta() {
  $("meetingMeta").textContent =
    `${S.meeting.title} · 开始 ${hm(S.meeting.start_time)} · 预计结束 ${hm(S.original.end)}` +
    (S.current.blocks.length ? ` · 新结束 ${hm(S.current.end)}` : "");
  $("btnUndo").disabled = S.entries.length === 0;
}

function renderItemList() {
  const list = $("itemList");
  list.innerHTML = "";
  $("listHint").classList.toggle("hidden", S.items.length > 0);

  S.items.forEach((it, idx) => {
    const li = document.createElement("li");
    li.className = `item-card ${it.kind === "break" ? "is-break" : "is-agenda"}`;
    const entry = S.entries.find((e) => e.item_id === it.id);
    if (entry) li.classList.add("recorded");

    const badges = [];
    if (it.kind === "break") {
      badges.push(`<span class="badge badge-break">休息 · 固定 ${hm(it.fixed_start)}</span>`);
    } else {
      badges.push(`<span class="badge badge-prio">P${it.priority}</span>`);
      if (it.incompressible) badges.push(`<span class="badge badge-lock">🔒 不可压缩</span>`);
    }
    if (entry) badges.push(`<span class="badge badge-done">已录入 ${hm(entry.actual_end)} 结束</span>`);

    const meta = it.kind === "break"
      ? `计划 ${it.planned_minutes} 分钟`
      : `计划 ${it.planned_minutes} 分 · 最短 ${it.min_minutes} 分`;

    li.innerHTML = `
      <div class="ic-head">
        <span class="ic-title"></span>
        <span class="ic-badges">${badges.join("")}</span>
      </div>
      <div class="ic-meta">${meta}</div>
      <div class="ic-actions">
        <button class="btn btn-sm" data-act="up" ${idx === 0 ? "disabled" : ""}>↑</button>
        <button class="btn btn-sm" data-act="down" ${idx === S.items.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn btn-sm btn-danger" data-act="del">删除</button>
        <span class="record-slot"></span>
      </div>`;
    li.querySelector(".ic-title").textContent = it.title;

    const slot = li.querySelector(".record-slot");
    if (it.kind === "agenda") {
      if (entry) {
        slot.innerHTML = `<span class="badge badge-done">${hm(entry.actual_end)}</span>`;
      } else if (S.next_recordable_id === it.id) {
        slot.style.display = "contents";
        slot.outerHTML = `
          <input type="datetime-local" data-role="entryTime" value="${defaultEntryTime()}">
          <button class="btn btn-sm btn-primary" data-act="record">录入实际结束</button>`;
      } else {
        slot.innerHTML = `<span class="hint">等待前序项录入</span>`;
      }
    }

    li.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "up" || act === "down") {
        api(`/api/items/${it.id}/move?direction=${act}`, { method: "POST" }).then(refresh, showError);
      } else if (act === "del") {
        if (confirm(`删除「${it.title}」？相关实况录入会一并移除。`)) {
          api(`/api/items/${it.id}`, { method: "DELETE" }).then(refresh, showError);
        }
      } else if (act === "record") {
        const input = li.querySelector('[data-role="entryTime"]');
        if (!input.value) { showError(new Error("请选择实际结束时间")); return; }
        api("/api/entries", {
          method: "POST",
          body: JSON.stringify({ item_id: it.id, actual_end: input.value }),
        }).then(refresh, showError);
      }
    });
    list.appendChild(li);
  });
}

function defaultEntryTime() {
  // 默认取会议开始 + 到该项为止的计划时长（即按计划结束的时刻）
  const blocks = S.original.blocks;
  const targetIdx = blocks.findIndex((b) => b.item_id === S.next_recordable_id);
  if (targetIdx >= 0) return toLocalInput(blocks[targetIdx].end_full);
  return toLocalInput(S.original.end);
}

// ---------- 时间轴 ----------

function timelineRange() {
  const times = [S.meeting.start_time];
  S.original.blocks.forEach((b) => times.push(b.start_full, b.end_full));
  if (S.current.blocks.length) S.current.blocks.forEach((b) => times.push(b.start_full, b.end_full));
  else times.push(S.original.end);
  const min = new Date(Math.min(...times.map((t) => new Date(t))));
  const max = new Date(Math.max(...times.map((t) => new Date(t))));
  // 两侧各留 10 分钟边距
  min.setMinutes(min.getMinutes() - 10);
  max.setMinutes(max.getMinutes() + 10);
  return { start: min, end: max };
}

function renderTimelines() {
  const range = timelineRange();
  const width = Math.max(480, minutesOf(range.end, range.start) * pxPerMin + LABEL_W);
  $("timelineInner").style.width = `${width}px`;

  const x = (iso) => LABEL_W + minutesOf(iso, range.start) * pxPerMin;
  const w = (b) => Math.max(2, minutesOf(b.end_full, b.start_full) * pxPerMin);

  // 刻度轴：按合适的间隔
  const axis = $("axisRow");
  axis.innerHTML = "";
  axis.style.width = `${width}px`;
  const spanMin = minutesOf(range.end, range.start);
  const step = spanMin <= 180 ? 15 : spanMin <= 360 ? 30 : 60;
  const t0 = new Date(range.start);
  t0.setMinutes(Math.ceil(t0.getMinutes() / step) * step, 0, 0);
  for (let t = t0; t <= range.end; t = new Date(t.getTime() + step * 60000)) {
    const tick = document.createElement("span");
    tick.className = "axis-tick";
    tick.style.left = `${x(localIso(t))}px`;
    tick.textContent = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
    axis.appendChild(tick);
  }

  const rows = [
    [$("originalRow"), S.original.blocks, false],
    [$("currentRow"), S.current.blocks, true],
  ];
  rows.forEach(([row, blocks, isCurrent]) => {
    row.innerHTML = "";
    row.style.width = `${width}px`;
    if (!blocks.length) {
      const empty = document.createElement("span");
      empty.className = "hint";
      empty.style.cssText = "position:absolute;left:96px;top:24px;";
      empty.textContent = isCurrent ? "录入第一项实际结束时间后生成" : "—";
      row.appendChild(empty);
      return;
    }
    blocks.forEach((b) => {
      const el = document.createElement("div");
      el.className = `tl-block ${b.kind} ${b.status}`;
      el.style.left = `${x(b.start_full)}px`;
      el.style.width = `${w(b)}px`;
      const durLabel = b.kind === "agenda" && (b.status === "compressed" || b.status === "conflict")
        ? `<span class="b-time">${hm(b.start_full)}–${hm(b.end_full)} · ${b.duration_minutes}分${
            b.saved_minutes ? `（省${b.saved_minutes}）` : ""}</span>`
        : `<span class="b-time">${hm(b.start_full)}–${hm(b.end_full)} · ${b.duration_minutes}分</span>`;
      el.innerHTML = `<span class="b-title"></span>${durLabel}`;
      el.querySelector(".b-title").textContent = b.title;
      el.title = `${b.title}\n${b.start_full.slice(11, 16)}–${b.end_full.slice(11, 16)} · ${b.duration_minutes} 分钟`;
      row.appendChild(el);
    });
  });

  // 当前时刻标记线（锚点 = 最近一次录入）
  document.querySelectorAll(".tl-nowline,.tl-endline").forEach((n) => n.remove());
  if (S.entries.length) {
    const last = S.entries[S.entries.length - 1];
    const line = document.createElement("div");
    line.className = "tl-nowline";
    line.style.left = `${x(last.actual_end)}px`;
    line.innerHTML = `<span>实际进度 ${hm(last.actual_end)}</span>`;
    $("timelineInner").appendChild(line);

    if (S.current.delay_minutes > 0.5) {
      const endLine = document.createElement("div");
      endLine.className = "tl-endline";
      endLine.style.left = `${x(S.current.end)}px`;
      endLine.innerHTML = `<span>新结束（延误 ${S.current.delay_minutes} 分）</span>`;
      $("timelineInner").appendChild(endLine);
    }
  }
  $("zoomLabel").textContent = `${pxPerMin.toFixed(1)} px/分`;
}

// ---------- 冲突与提示 ----------

function renderBanners() {
  const conflicts = (S.current.conflicts || []).filter((n) => n.type !== "missed_break");
  const missed = (S.current.conflicts || []).filter((n) => n.type === "missed_break");
  const banner = $("conflictBanner");
  const detail = $("conflictDetail");

  if (conflicts.length) {
    const overlap = conflicts.find((c) => c.type === "break_overlap");
    banner.classList.remove("hidden");
    banner.innerHTML = `<strong>⚠ 出现排期冲突：议程无法在固定休息段前全部容纳</strong>` +
      (overlap ? `议程整体溢出 <b>${overlap.overflow_minutes}</b> 分钟，与固定休息段「${overlap.title}」（${overlap.break_start.slice(11)}–${overlap.break_end.slice(11)}）重叠。` : "");

    const itemConflicts = conflicts.filter((c) => c.type === "item");
    detail.classList.remove("hidden");
    detail.innerHTML = `<h3>冲突项明细</h3>
      <table>
        <thead><tr><th>议程项</th><th>优先级</th><th>实际安排时长</th><th>超出分钟数</th><th>说明</th></tr></thead>
        <tbody>
          ${itemConflicts.map((c) => `<tr>
            <td>${c.title}${c.incompressible ? " 🔒" : ""}</td>
            <td>P${c.priority}</td>
            <td>${c.duration_minutes} 分</td>
            <td><b>+${c.overflow_minutes}</b> 分</td>
            <td>${c.start.slice(11)}–${c.end.slice(11)}，越过休息段起点 ${c.window_end.slice(11)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      <div class="total">总超出：${overlap ? overlap.overflow_minutes : itemConflicts.reduce((s, c) => s + c.overflow_minutes, 0)} 分钟</div>
      ${missed.map((m) => `<div class="missed-note">· 固定休息段「${m.title}」（${m.fixed_start.slice(11)} 开始）已被错过，按规则保持原时间不移动。</div>`).join("")}`;
  } else {
    banner.classList.add("hidden");
    detail.classList.add("hidden");
  }

  const notice = $("noticeBanner");
  if (!conflicts.length && missed.length) {
    notice.classList.remove("hidden");
    notice.innerHTML = missed.map((m) =>
      `固定休息段「${m.title}」（${m.fixed_start.slice(11)} 开始）已被错过，保持原时间不移动。`).join("<br>");
  } else {
    notice.classList.add("hidden");
  }
}

// ---------- 交互绑定 ----------

function showError(err) {
  alert(err.message);
}

function refresh() {
  return api("/api/state").then((s) => { S = s; render(); });
}

$("itemForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const kind = $("fKind").value;
  const planned = parseFloat($("fPlanned").value);
  const minimum = parseFloat($("fMin").value || "0");
  const body = {
    kind,
    title: $("fTitle").value.trim(),
    planned_minutes: planned,
    min_minutes: kind === "agenda" ? minimum : 0,
    priority: parseInt($("fPriority").value, 10),
    incompressible: $("fIncompressible").checked,
    fixed_start: kind === "break" ? $("fFixedStart").value : null,
  };
  if (kind === "break" && !body.fixed_start) { showError(new Error("休息段必须选择固定开始时间")); return; }
  api("/api/items", { method: "POST", body: JSON.stringify(body) })
    .then(() => { ev.target.reset(); $("fPlanned").value = 20; syncKindFields(); refresh(); })
    .catch(showError);
});

function syncKindFields() {
  const isBreak = $("fKind").value === "break";
  document.querySelectorAll(".agenda-only").forEach((el) => el.classList.toggle("hidden", isBreak));
  document.querySelectorAll(".break-only").forEach((el) => el.classList.toggle("hidden", !isBreak));
}
$("fKind").addEventListener("change", syncKindFields);

$("btnUndo").addEventListener("click", () => {
  api("/api/entries/undo", { method: "POST" }).then(refresh, showError);
});

$("btnReset").addEventListener("click", () => {
  const now = new Date();
  now.setMinutes(0, 0, 0);
  now.setHours(now.getHours() + 1);
  $("rStart").value = toLocalInput(now.toISOString()).slice(0, 16);
  $("resetDialog").showModal();
});

$("confirmReset").addEventListener("click", (ev) => {
  ev.preventDefault();
  api("/api/meeting/reset", {
    method: "POST",
    body: JSON.stringify({ title: $("rTitle").value.trim() || "团队例会", start_time: $("rStart").value }),
  }).then(() => { $("resetDialog").close(); refresh(); }).catch(showError);
});

$("zoomIn").addEventListener("click", () => { pxPerMin = Math.min(8, pxPerMin + 0.5); renderTimelines(); });
$("zoomOut").addEventListener("click", () => { pxPerMin = Math.max(0.6, pxPerMin - 0.5); renderTimelines(); });

// ---------- 启动 ----------

refresh();
