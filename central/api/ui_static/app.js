/* 중앙증권 대시보드. 빌드 없이 쓰는 단일 스크립트.
   화면 = 레이아웃(pages[].widgets[]) 이고, 위젯은 /ui/api/w/{type} 데이터를 주기적으로 받아 그린다. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const S = { me: null, layout: null, custom: false, page: null, timers: [], catalog: {}, vars: {}, widgets: [] };

// ───────────── 공통 ─────────────
async function api(path, opts = {}) {
  const res = await fetch("/ui/api" + path, {
    credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* 본문 없음 */ }
  if (res.status === 401) { showLogin(); throw new Error("로그인이 필요함"); }
  if (!body || !body.ok) throw new Error(body && body.error ? `[${body.error.code}] ${body.error.message}` : `HTTP ${res.status}`);
  return body.data;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(v) { return v === null || v === undefined || v === "" ? null : Number(v); }
function fmt(v, digits) {
  const n = num(v);
  if (n === null || Number.isNaN(n)) return "-";
  const d = digits ?? (Math.abs(n) >= 1000 || Number.isInteger(n) ? 0 : 2);
  return n.toLocaleString("ko-KR", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function money(v, cur) { return fmt(v, cur === "USD" ? 2 : 0); }
function signed(v, cur) {
  const n = num(v);
  if (n === null) return '<span class="muted">-</span>';
  const cls = n > 0 ? "up" : n < 0 ? "down" : "";
  return `<span class="${cls}">${n > 0 ? "+" : ""}${money(n, cur)}</span>`;
}
function pct(v, digits = 1) {
  const n = num(v);
  return n === null ? "-" : `${(n * 100).toFixed(digits)}%`;
}
function hms(iso) { return iso ? String(iso).slice(11, 19) : "-"; }
function dt(iso) { return iso ? String(iso).slice(5, 16).replace("T", " ") : "-"; }
function since(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}초`;
  if (s < 5400) return `${Math.round(s / 60)}분`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}시간`;
  return `${(s / 86400).toFixed(1)}일`;
}
function badge(status) { return `<span class="badge ${esc(status)}">${esc(STATUS_KO[status] || status)}</span>`; }
const STATUS_KO = {
  SUBMITTING: "전송중", ACCEPTED: "접수", PARTIALLY_FILLED: "부분체결", FILLED: "체결", CANCEL_PENDING: "취소중",
  CANCELED: "취소", REJECTED: "거부", SUBMIT_FAILED: "전송실패", UNKNOWN: "미확인", EXPIRED: "만료", REPLACED: "정정됨",
};
const SIDE_KO = { buy: '<span class="up">매수</span>', sell: '<span class="down">매도</span>' };
const KIND_KO = { order: "주문", account: "계좌조회", quote: "시세", ranking: "순위/수급", other: "기타" };
const PALETTE = ["#2f6fde", "#d93a3a", "#1f9d55", "#c98a00", "#8a4fd8", "#0f9bb0", "#d65c9a", "#6b7484"];
// 앱 구분 색. 매수/매도(빨강/파랑)와 헷갈리지 않게 빨강, 파랑 계열은 뺀다. 같은 앱은 모든 위젯에서 같은 색
const APP_COLORS = ["#1f9d55", "#e07b39", "#8a4fd8", "#0f9bb0", "#d65c9a", "#a07c00", "#795548", "#6b7484"];
function subColor(id) {
  const i = S.me ? S.me.subs.findIndex(s => s.id === id) : -1;
  return APP_COLORS[(i >= 0 ? i : id) % APP_COLORS.length];
}

function table(cols, rows, opts = {}) {
  if (!rows.length) return `<div class="empty">${esc(opts.empty || "없음")}</div>`;
  const head = cols.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => {
    const attrs = opts.rowAttrs ? opts.rowAttrs(r, i) : "";
    return `<tr ${attrs}>${cols.map(c => `<td class="${c.num ? "num" : ""} ${c.cls || ""}">${c.html(r)}</td>`).join("")}</tr>`;
  }).join("");
  return `<div class="scroll ${opts.tall ? "tall" : ""}"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ───────────── 차트 (SVG) ─────────────
function niceTicks(lo, hi, n = 4) {
  const raw = (hi - lo) / n || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) ticks.push(Math.round(v / step) * step || 0);
  return ticks;
}
function svgOpen(W, H) { return `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`; }
function widthOf(el) { return Math.max(280, (el && el.clientWidth ? el.clientWidth : 800) - 24); }

function lineChart(series, { height = 220, fmtY = v => fmt(v), width = 800 } = {}) {
  const pts = series.flatMap(s => s.points);
  if (!pts.length) return '<div class="empty">데이터 없음</div>';
  const W = width, H = height, L = 64, R = 12, T = 10, B = 24;
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]).concat([0]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs);
  if (x0 === x1) { x0 -= 60000; x1 += 60000; }
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - R);
  const Y = y => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  let g = "";
  niceTicks(y0, y1).forEach(v => {
    const y = Y(v);
    g += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text x="${L - 6}" y="${y + 4}" text-anchor="end">${esc(fmtY(v))}</text>`;
  });
  const nx = Math.max(2, Math.min(6, Math.floor(W / 140)));
  for (let i = 0; i <= nx; i++) {
    const x = x0 + (x1 - x0) * i / nx;
    const d = new Date(x);
    const label = (x1 - x0) > 3 * 86400000 ? `${d.getMonth() + 1}/${d.getDate()}` : d.toTimeString().slice(0, 5);
    g += `<text x="${X(x)}" y="${H - 6}" text-anchor="middle">${label}</text>`;
  }
  g += `<line class="zero" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  series.forEach((s, i) => {
    if (!s.points.length) return;
    const color = s.color || PALETTE[i % PALETTE.length];
    let d = "";  // 계단형: 체결 시점에 값이 바뀐다
    s.points.forEach((p, j) => {
      d += j === 0 ? `M${X(p[0])},${Y(p[1])}` : `H${X(p[0])}V${Y(p[1])}`;
    });
    const last = s.points[s.points.length - 1];
    g += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"/>`;
    g += `<circle cx="${X(last[0])}" cy="${Y(last[1])}" r="3" fill="${color}"><title>${esc(s.name)} ${esc(fmtY(last[1]))}</title></circle>`;
  });
  const legend = series.map((s, i) => `<span><i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>${esc(s.name)}</span>`).join("");
  return `${svgOpen(W, H)}${g}</svg><div class="legend">${legend}</div>`;
}

function barChart(items, { height = 160, fmtY = v => fmt(v), signedColors = true, width = 800 } = {}) {
  if (!items.length) return '<div class="empty">데이터 없음</div>';
  const W = width, H = height, L = 64, R = 8, T = 8, B = 22;
  const vals = items.map(i => i.value).concat([0]);
  let y0 = Math.min(...vals), y1 = Math.max(...vals);
  if (y0 === y1) y1 = y0 + 1;
  const Y = y => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const bw = Math.min(48, (W - L - R) / items.length);
  let g = `<text x="${L - 6}" y="${Y(y1) + 4}" text-anchor="end">${esc(fmtY(y1))}</text>`;
  if (y0 < 0) g += `<text x="${L - 6}" y="${Y(y0) + 4}" text-anchor="end">${esc(fmtY(y0))}</text>`;
  g += `<line class="zero" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  const every = Math.max(1, Math.ceil(items.length / Math.max(2, Math.floor((W - L) / 56))));
  items.forEach((it, i) => {
    const x = L + i * bw + bw * 0.15, w = Math.max(1, bw * 0.7);
    const top = Y(Math.max(0, it.value)), h = Math.abs(Y(it.value) - Y(0));
    const color = it.color || (signedColors ? (it.value >= 0 ? "var(--up)" : "var(--down)") : "var(--accent)");
    g += `<rect x="${x}" y="${top}" width="${w}" height="${Math.max(h, 0.5)}" fill="${color}"><title>${esc(it.label)}: ${esc(fmtY(it.value))}</title></rect>`;
    if (i % every === 0) g += `<text x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(it.short || it.label)}</text>`;
  });
  return `${svgOpen(W, H)}${g}</svg>`;
}

function stackedBars(perKind, { height = 90, width = 800, colors = null, unit = "" } = {}) {
  const kinds = Object.keys(perKind);
  if (!kinds.length) return '<div class="empty">최근 1시간 호출 없음</div>';
  const color = ki => (colors && colors[ki]) || PALETTE[ki % PALETTE.length];
  const n = perKind[kinds[0]].length;
  const totals = Array.from({ length: n }, (_, i) => kinds.reduce((a, k) => a + perKind[k][i], 0));
  const max = Math.max(1, ...totals);
  const W = width, H = height, L = 40, B = 16, bw = (W - L) / n;
  let g = `<text x="${L - 4}" y="12" text-anchor="end">${max}</text><text x="${L - 4}" y="${H - B}" text-anchor="end">0</text>`;
  for (let i = 0; i < n; i++) {
    let y = H - B;
    kinds.forEach((k, ki) => {
      const v = perKind[k][i];
      if (!v) return;
      const h = v / max * (H - B - 4);
      y -= h;
      g += `<rect x="${L + i * bw + 1}" y="${y}" width="${Math.max(1, bw - 2)}" height="${h}" fill="${color(ki)}"><title>${60 - i}분 전 ${esc(KIND_KO[k] || k)} ${v}${unit}</title></rect>`;
    });
  }
  g += `<text x="${L}" y="${H - 3}">60분 전</text><text x="${W}" y="${H - 3}" text-anchor="end">지금</text>`;
  const legend = kinds.map((k, i) => `<span><i style="background:${color(i)}"></i>${esc(KIND_KO[k] || k)}</span>`).join("");
  return `${svgOpen(W, H)}${g}</svg><div class="legend">${legend}</div>`;
}

// 캔들: 국내 관례대로 양봉 빨강, 음봉 파랑. 체결은 체결가 위치에 매수 ▲(빨강), 매도 ▼(파랑)
function candleChart(d, { width = 800, priceH = 260, volH = 56, multiApp = null } = {}) {
  const daily = d.interval === "1d";
  const bars = d.candles.map(c => ({ t: new Date(c[0]).getTime(), iso: c[0], o: num(c[1]), h: num(c[2]), l: num(c[3]), c: num(c[4]), v: num(c[5]) }));
  if (!bars.length) return null;
  const W = width, L = 6, R = 66, T = 8, GAP = 18, B = 20;
  const H = T + priceH + GAP + volH + B;
  const n = bars.length, slot = (W - L - R) / n, bw = Math.max(1, Math.min(14, slot * 0.7));
  const X = i => L + slot * (i + 0.5);

  // 체결을 봉에 붙인다 (봉 시작 시각 <= 체결 시각인 마지막 봉). 같은 봉, 같은 방향은 하나로 묶는다
  const idxOf = t => {
    let lo = 0, hi = n - 1, ans = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (bars[m].t <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
    return ans;
  };
  const groups = {};
  d.markers.forEach(m => {
    const i = idxOf(new Date(m.ts).getTime());
    if (i < 0) return;
    const k = `${i}:${m.side}:${m.sub_id}`;
    const g = groups[k] || (groups[k] = { i, side: m.side, sub_id: m.sub_id, sub: m.sub, qty: 0, amount: 0, pnl: 0, items: [] });
    g.qty += m.qty; g.amount += num(m.price) * m.qty; g.pnl += num(m.pnl); g.items.push(m);
  });
  const marks = Object.values(groups).map(g => ({ ...g, price: g.amount / g.qty }));
  const avgs = d.positions.map(p => ({ sub: p.sub, sub_id: p.sub_id, price: num(p.avg_price) }));
  // 앱이 둘 이상이면 앱마다 색을 달리하고, 같은 봉에서는 옆으로 조금씩 비켜 그린다. 방향(▲ 매수, ▼ 매도)은 그대로
  const apps = [...new Set(d.markers.map(m => m.sub_id).concat(avgs.map(a => a.sub_id)))];
  const multi = multiApp ?? apps.length > 1;
  const appOffset = id => multi ? (apps.indexOf(id) - (apps.length - 1) / 2) * 9 : 0;
  const last = num(d.last);

  const ys = bars.flatMap(b => [b.h, b.l]).concat(marks.map(m => m.price), avgs.map(a => a.price));
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
  const Y = y => T + (1 - (y - y0) / (y1 - y0)) * priceH;
  const vmax = Math.max(1, ...bars.map(b => b.v));
  const vTop = T + priceH + GAP;
  const VY = v => vTop + volH - v / vmax * volH;
  const fy = v => fmt(v, d.currency === "USD" ? 2 : 0);

  let g = "";
  niceTicks(y0, y1, 5).forEach(v => {
    if (v < y0 || v > y1) return;
    if (last !== null && Math.abs(Y(v) - Y(last)) < 14) return;   // 현재가 표시와 겹치는 눈금은 뺀다
    g += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${W - R + 6}" y="${Y(v) + 4}">${esc(fy(v))}</text>`;
  });
  g += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${vTop + volH}" y2="${vTop + volH}"/>`;
  g += `<text x="${W - R + 6}" y="${vTop + 9}">${esc(fmt(vmax, 0))}</text>`;
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - L - R) / 70))));
  for (let i = 0; i < n; i += every) {
    if (X(i) < 18) continue;   // 왼쪽 끝에서 잘리는 글자는 뺀다
    const label = daily ? bars[i].iso.slice(5, 10).replace("-", "/") : bars[i].iso.slice(11, 16);
    g += `<text x="${X(i)}" y="${H - 5}" text-anchor="middle">${label}</text>`;
  }
  bars.forEach((b, i) => {
    const col = b.c >= b.o ? "var(--up)" : "var(--down)";
    const top = Y(Math.max(b.o, b.c)), h = Math.max(1, Math.abs(Y(b.o) - Y(b.c)));
    g += `<line x1="${X(i)}" x2="${X(i)}" y1="${Y(b.h)}" y2="${Y(b.l)}" stroke="${col}"/>`;
    g += `<rect x="${X(i) - bw / 2}" y="${top}" width="${bw}" height="${h}" fill="${col}"/>`;
    g += `<rect x="${X(i) - bw / 2}" y="${VY(b.v)}" width="${bw}" height="${vTop + volH - VY(b.v)}" fill="${col}" opacity=".45"/>`;
  });
  avgs.forEach((a, k) => {
    const col = multi ? subColor(a.sub_id) : "var(--warn)";
    g += `<line x1="${L}" x2="${W - R}" y1="${Y(a.price)}" y2="${Y(a.price)}" stroke="${col}" stroke-dasharray="5 4"/>`;
    g += `<text x="${L + 4 + k * 150}" y="${Y(a.price) - 4}" style="fill:${col}">평단 ${esc(a.sub)} ${esc(fy(a.price))}</text>`;
  });
  if (last !== null && last >= y0 && last <= y1) {
    g += `<line x1="${L}" x2="${W - R}" y1="${Y(last)}" y2="${Y(last)}" stroke="var(--muted)" stroke-dasharray="1 3"/>`;
    g += `<rect x="${W - R + 2}" y="${Y(last) - 8}" width="${R - 4}" height="16" rx="3" fill="var(--text)"/>`;
    g += `<text x="${W - R + 6}" y="${Y(last) + 4}" style="fill:var(--card)">${esc(fy(last))}</text>`;
  }
  marks.forEach(m => {
    const x = X(m.i) + appOffset(m.sub_id), y = Y(m.price), s = 7;
    const buy = m.side === "buy";
    const path = buy ? `M${x},${y} l${-s},${s * 1.6} h${2 * s} z` : `M${x},${y} l${-s},${-s * 1.6} h${2 * s} z`;
    const tip = `${m.sub} ${buy ? "매수" : "매도"} ${fmt(m.qty)}주 @ ${fy(m.price)} (${m.items.length}건)` +
      `\n${m.items.map(x => `${hms(x.ts)} #${x.order_id ?? "-"} ${fmt(x.qty)} @ ${fy(x.price)}${!buy ? ` 손익 ${fmt(x.pnl)}` : ""}`).join("\n")}`;
    g += `<path class="mark" d="${path}" fill="${multi ? subColor(m.sub_id) : buy ? "var(--up)" : "var(--down)"}" stroke="var(--card)" stroke-width="1" data-order="${m.items[m.items.length - 1].order_id ?? ""}"><title>${esc(tip)}</title></path>`;
  });
  g += `<line class="xhair" x1="0" x2="0" y1="${T}" y2="${vTop + volH}" stroke="var(--muted)" stroke-dasharray="2 2" visibility="hidden"/>`;
  g += `<rect class="hover" x="${L}" y="${T}" width="${W - L - R}" height="${vTop + volH - T}" fill="transparent"/>`;
  return { html: `${svgOpen(W, H)}${g}</svg>`, bars, X, slot, L, daily, multi };
}

// ───────────── 위젯 ─────────────
const W = {};

W.status = {
  render(body, d) {
    const alerts = d.alerts.length
      ? d.alerts.map(a => `<div class="alert ${a.level === "error" ? "error" : ""}">${esc(a.text)}</div>`).join("")
      : '<div class="alert fine">이상 없음</div>';
    const tiles = d.brokers.map(b => `
      <div class="tile">
        <div class="k">${esc(b.alias)} (${b.env === "real" ? "실전" : "모의"})</div>
        <div class="v"><span class="dot ${b.connected ? "ok" : "err"}"></span> ${b.connected ? "연결됨" : "끊김"}
          · ${b.trading_enabled ? '<span class="ok">거래 켬</span>' : '<span class="muted">거래 끔</span>'}</div>
        <div class="muted">실시간 ${b.slots_used ?? "-"}/${b.slots_limit ?? "-"} · 접속 ${b.connects ?? "-"}회</div>
      </div>`).join("");
    const recon = d.reconciliations.map(r => `
      <div class="tile"><div class="k">대사 ${esc(r.broker)}/${esc(r.market)}</div>
        <div class="v ${r.ok ? "ok" : "error"}">${r.ok ? "일치" : "불일치"}</div><div class="muted">${dt(r.run_at)}</div></div>`).join("");
    let extra = `<div class="tile"><div class="k">미확인 주문</div><div class="v ${d.unknown_orders ? "error" : ""}">${d.unknown_orders}</div>
      <div class="muted">정지 ${d.halted.length ? d.halted.map(h => esc(h.scope + " " + h.id)).join(", ") : "없음"}</div></div>`;
    if (d.log) extra += `<div class="tile"><div class="k">오늘 로그</div>
      <div class="v"><span class="${d.log.errors ? "error" : ""}">ERROR ${d.log.errors}</span> · <span class="${d.log.warnings ? "warn" : ""}">WARN ${d.log.warnings}</span></div>
      <div class="muted">서버 ${since(d.log.since)} 전 시작</div></div>`;
    if (d.programs) extra += `<div class="tile"><div class="k">프로그램</div><div class="chips">${d.programs.children.map(c =>
      `<span class="chip"><span class="dot ${c.running ? "ok" : "err"}"></span>${esc(c.name)}</span>`).join("")}</div></div>`;
    body.innerHTML = `<div class="alerts">${alerts}</div><div class="tiles">${tiles}${extra}${recon}</div>`;
  },
};

W.accounts = {
  render(body, d) {
    const rows = [];
    d.rows.forEach(r => {
      const curs = Object.keys(r.currencies);
      if (!curs.length) curs.push("KRW");
      curs.forEach((c, i) => rows.push({ r, c, first: i === 0, v: r.currencies[c] || {}, p: (r.pnl || {})[c] || {} }));
    });
    body.classList.add("flush");
    body.innerHTML = table([
      { label: "서브계좌", html: x => x.first ? `<b>${esc(x.r.name)}</b> <span class="muted">#${x.r.id}</span>${x.r.status !== "active" ? ` <span class="badge err">${esc(x.r.status)}</span>` : ""}` : "" },
      { label: "실계좌", html: x => x.first ? `${esc(x.r.broker)}` : "" },
      { label: "통화", html: x => esc(x.c) },
      { label: "현금", num: true, html: x => money(x.v.cash, x.c) },
      { label: "주문가능", num: true, html: x => money(x.v.available, x.c) },
      { label: "보유평가", num: true, html: x => money(x.v.market_value, x.c) + (x.v.unpriced ? ` <span class="muted" title="실시간 시세 없는 종목은 매입가로 계산">*${x.v.unpriced}</span>` : "") },
      { label: "당일 실현손익", num: true, html: x => signed(x.p.realized_pnl, x.c) },
      { label: "수수료+세금", num: true, html: x => x.p.fee !== undefined ? money(num(x.p.fee) + num(x.p.tax), x.c) : "-" },
      { label: "주문/체결/거부", num: true, html: x => x.first ? `${x.r.day.orders} / ${x.r.day.filled} / <span class="${x.r.day.rejected ? "error" : ""}">${x.r.day.rejected}</span>` : "" },
      { label: "거부율", num: true, html: x => x.first ? `<span class="${(x.r.day.reject_rate || 0) >= 0.3 ? "error" : ""}">${pct(x.r.day.reject_rate, 0)}</span>` : "" },
      { label: "미확인", num: true, html: x => x.first && x.r.day.unknown ? `<span class="error">${x.r.day.unknown}</span>` : "" },
    ], rows, { rowAttrs: x => x.first ? "" : 'class="sub-row"' });
  },
};

W.pnl_curve = {
  render(body, d) {
    const byCur = {};
    d.series.forEach(s => (byCur[s.currency] = byCur[s.currency] || []).push({
      name: s.name, color: subColor(s.sub_id), points: s.points.map(p => [new Date(p[0]).getTime(), num(p[1])]) }));
    const curs = Object.keys(byCur);
    body.innerHTML = curs.length ? curs.map(c => `<div class="muted">${esc(c)} 누적 실현손익 (수수료, 세금 차감)</div>` +
      lineChart(byCur[c], { fmtY: v => money(v, c), width: widthOf(body) })).join("") : '<div class="empty">오늘 체결 없음</div>';
  },
};

W.pnl_daily = {
  render(body, d) {
    if (!d.series.length) { body.innerHTML = '<div class="empty">기간 내 체결 없음</div>'; return; }
    const summary = table([
      { label: "서브계좌", html: s => `<b>${esc(s.name)}</b>` },
      { label: "통화", html: s => esc(s.currency) },
      { label: "실현손익", num: true, html: s => signed(s.total.pnl, s.currency) },
      { label: "수수료", num: true, html: s => money(s.total.fee, s.currency) },
      { label: "세금", num: true, html: s => money(s.total.tax, s.currency) },
      { label: "체결", num: true, html: s => fmt(s.total.fills) },
      { label: "매도 승/패", num: true, html: s => `${s.total.wins} / ${s.total.losses}` },
      { label: "승률", num: true, html: s => pct(s.total.win_rate, 0) },
      { label: "거래대금", num: true, html: s => money(num(s.total.buy_amount) + num(s.total.sell_amount), s.currency) },
    ], d.series);
    const charts = d.series.map(s => `<div style="margin-top:10px"><div class="muted">${esc(s.name)} ${esc(s.currency)} 일별 실현손익</div>` +
      barChart(s.days.map(x => ({ label: x.date, short: x.date.slice(5), value: num(x.pnl) })), { fmtY: v => money(v, s.currency), width: widthOf(body) }) + "</div>").join("");
    body.innerHTML = `<div class="muted">${esc(d.from)} ~ ${esc(d.to)}</div>${summary}${charts}`;
  },
};

W.blotter = {
  controls(card, spec, refresh) {
    const f = document.createElement("div");
    f.className = "filters";
    const pageSub = (currentPage().vars || []).includes("sub");
    const subs = S.me.role === "admin" && !pageSub ? `<select data-k="sub"><option value="">전체 서브계좌</option>${S.me.subs.map(s =>
      `<option value="${s.id}">${esc(s.name)} #${s.id}</option>`).join("")}</select>` : "";
    f.innerHTML = `<input type="date" data-k="date">${subs}
      <select data-k="status"><option value="">전체 상태</option><option value="FILLED,PARTIALLY_FILLED">체결</option>
        <option value="ACCEPTED,PARTIALLY_FILLED,SUBMITTING,CANCEL_PENDING">미체결</option>
        <option value="REJECTED,SUBMIT_FAILED">거부/실패</option><option value="UNKNOWN">미확인</option>
        <option value="CANCELED,REPLACED">취소/정정</option></select>
      <input data-k="symbol" placeholder="종목 (KR:005930)" size="14">
      <span class="chips" data-counts></span>`;
    f.querySelectorAll("[data-k]").forEach(el => {
      if (spec.params[el.dataset.k]) el.value = spec.params[el.dataset.k];
      el.addEventListener("change", () => { spec.live[el.dataset.k] = el.value; refresh(); });
    });
    card.insertBefore(f, card.querySelector(".card-body"));
  },
  render(body, d, spec, card) {
    const counts = Object.entries(d.counts).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="chip">${badge(k)} ${v}</span>`).join("");
    const c = card.querySelector("[data-counts]");
    if (c) c.innerHTML = counts;
    const dateInput = card.querySelector('[data-k="date"]');
    if (dateInput && !spec.live.date) dateInput.value = d.date;   // 페이지 날짜를 바꾸면 따라간다
    body.classList.add("flush");
    body.innerHTML = table([
      { label: "시각", html: r => `<span class="mono">${hms(r.ts)}</span>` },
      { label: "#", num: true, html: r => r.id },
      { label: "앱", html: r => esc(r.sub) },
      { label: "종목", html: r => symLink(r.symbol, r.name, r.symbol.split(":")[1] || "") },
      { label: "구분", html: r => SIDE_KO[r.side] || esc(r.side) },
      { label: "유형", html: r => r.type === "market" ? "시장가" : "지정가" },
      { label: "가격", num: true, html: r => r.price ? fmt(r.price) : "-" },
      { label: "수량", num: true, html: r => fmt(r.qty) },
      { label: "체결", num: true, html: r => r.filled ? `${fmt(r.filled)} @ ${fmt(r.avg_price)}` : "-" },
      { label: "상태", html: r => badge(r.status) },
      { label: "접수", num: true, html: r => r.accept_ms === null ? "-" : `<span class="${r.accept_ms > 3000 ? "warn" : ""}">${(r.accept_ms / 1000).toFixed(2)}초</span>` },
      { label: "사유", cls: "wrap", html: r => r.reason ? `<span class="error">${esc(r.reason)}</span>` : "" },
    ], d.rows, { tall: true, empty: "주문 없음", rowAttrs: r => `class="click" data-order="${r.id}"` });
    body.querySelectorAll("tr[data-order]").forEach(tr => tr.addEventListener("click", () => openOrder(tr.dataset.order)));
    bindSymLinks(body);
  },
};

W.positions = {
  render(body, d) {
    body.classList.add("flush");
    body.innerHTML = table([
      { label: "앱", html: r => esc(r.sub) },
      { label: "종목", html: r => symLink(r.symbol, r.name, r.symbol) },
      { label: "수량", num: true, html: r => fmt(r.qty) + (r.sellable !== r.qty ? ` <span class="muted">(매도가능 ${fmt(r.sellable)})</span>` : "") },
      { label: "평균단가", num: true, html: r => fmt(r.avg_price) },
      { label: "현재가", num: true, html: r => r.last ? fmt(r.last) : '<span class="muted" title="실시간 시세 없음">-</span>' },
      { label: "평가손익", num: true, html: r => signed(r.unrealized, r.currency) },
      { label: "수익률", num: true, html: r => r.unrealized_rate === null ? "-" : `<span class="${r.unrealized_rate > 0 ? "up" : r.unrealized_rate < 0 ? "down" : ""}">${pct(r.unrealized_rate, 2)}</span>` },
    ], d.rows, { empty: "보유 종목 없음" });
    bindSymLinks(body);
  },
};

W.calls = {
  render(body, d) {
    body.innerHTML = d.brokers.map(b => {
      const st = b.stats;
      if (!st) return `<div><b>${esc(b.alias)}</b> <span class="muted">통계 없음</span></div>`;
      const win = ["1m", "10m", "60m"].map(k => ({ k, ...st.windows[k] }));
      const t = table([
        { label: "구간", html: r => ({ "1m": "최근 1분", "10m": "최근 10분", "60m": "최근 1시간" })[r.k] },
        { label: "호출", num: true, html: r => fmt(r.calls) },
        { label: "분당", num: true, html: r => fmt(r.calls / parseInt(r.k), 1) },
        { label: "대기 평균", num: true, html: r => `${fmt(r.wait_avg_ms, 0)}ms` },
        { label: "대기 p95", num: true, html: r => `<span class="${r.wait_p95_ms > 2000 ? "warn" : ""}">${fmt(r.wait_p95_ms, 0)}ms</span>` },
        { label: "응답 평균", num: true, html: r => `${fmt(r.elapsed_avg_ms, 0)}ms` },
        { label: "한도초과", num: true, html: r => r.rate_limited ? `<span class="warn">${r.rate_limited}</span>` : "0" },
        { label: "오류", num: true, html: r => r.errors ? `<span class="error">${r.errors}</span>` : "0" },
      ], win);
      const kinds = table([
        { label: "종류 (1시간)", html: r => esc(KIND_KO[r[0]] || r[0]) },
        { label: "호출", num: true, html: r => fmt(r[1].calls) },
        { label: "대기 평균", num: true, html: r => `${fmt(r[1].wait_avg_ms, 0)}ms` },
        { label: "응답 평균", num: true, html: r => `${fmt(r[1].elapsed_avg_ms, 0)}ms` },
        { label: "오류", num: true, html: r => fmt(r[1].errors) },
      ], Object.entries(st.by_kind));
      return `<div style="margin-bottom:14px"><div class="row"><b>${esc(b.alias)}</b>
        <span class="muted">${b.env === "real" ? "실전" : "모의"} · 한도 초당 ${fmt(b.rate_limit_per_sec)}건 · 실시간 ${b.slots_used ?? "-"}/${b.slots_limit ?? "-"}</span></div>
        ${stackedBars(st.per_minute_kind, { width: widthOf(body) })}<div class="row" style="align-items:flex-start;margin-top:6px">
        <div style="flex:3;min-width:300px">${t}</div><div style="flex:2;min-width:240px">${kinds}</div></div></div>`;
    }).join("");
  },
};

W.errors = {
  render(body, d) {
    const groups = table([
      { label: "횟수", num: true, html: g => fmt(g.count) },
      { label: "수준", html: g => `<span class="${g.level === "WARNING" ? "warn" : "error"}">${esc(g.level)}</span>` },
      { label: "위치", html: g => `<span class="muted">${esc(g.logger)}</span>` },
      { label: "메시지 (마지막)", cls: "wrap", html: g => esc(g.message) + (g.exc ? `<div class="muted">${esc(g.exc)}</div>` : "") },
      { label: "마지막", html: g => `<span class="mono">${dt(g.last)}</span>` },
    ], d.groups, { empty: "오류와 경고 없음" });
    const recent = table([
      { label: "시각", html: i => `<span class="mono">${dt(i.ts)}</span>` },
      { label: "수준", html: i => `<span class="${i.level === "WARNING" ? "warn" : "error"}">${esc(i.level)}</span>` },
      { label: "위치", html: i => `<span class="muted">${esc(i.logger)}</span>` },
      { label: "메시지", cls: "wrap", html: i => esc(i.message) + (i.exc ? `<div class="muted">${esc(i.exc)}</div>` : "") },
    ], d.recent, { tall: true });
    body.innerHTML = `<div class="muted">집계 시작 ${dt(d.since)} (서버 재시작 때 비워짐)</div>${groups}
      <details style="margin-top:8px"><summary>최근 ${d.recent.length}건</summary>${recent}</details>`;
  },
};

W.jobs = {
  render(body, d) {
    const jobs = table([
      { label: "작업", html: j => `<b>${esc(j.name)}</b>${j.running ? ' <span class="badge">실행중</span>' : ""}` },
      { label: "시각", html: j => j.at ? `${esc(j.at)} <span class="muted">${esc((j.tz || "").replace("America/New_York", "뉴욕").replace("Asia/Seoul", ""))}</span>` : "-" },
      { label: "마지막", html: j => `<span class="mono">${dt(j.finished_at)}</span>` },
      { label: "결과", html: j => j.ok === null ? "-" : j.ok ? '<span class="ok">성공</span>' : '<span class="error">실패</span>' },
      { label: "내용", cls: "wrap", html: j => `<span class="muted">${esc(j.detail || j.description || "")}</span>` },
    ], d.jobs);
    const recon = table([
      { label: "대사", html: r => `${esc(r.broker_account_id)}/${esc(r.market)}` },
      { label: "시각", html: r => `<span class="mono">${dt(r.run_at)}</span>` },
      { label: "결과", html: r => r.ok ? '<span class="ok">일치</span>' : '<span class="error">불일치</span>' },
      { label: "차이", cls: "wrap", html: r => r.diffs && r.diffs.length ? esc(JSON.stringify(r.diffs).slice(0, 300)) : "" },
    ], d.reconciliations);
    body.innerHTML = jobs + `<div style="margin-top:10px">${recon}</div>`;
  },
};

W.programs = {
  render(body, d) {
    const sv = d.supervisor;
    if (!sv) { body.innerHTML = '<div class="empty">감시자 상태 파일 없음</div>'; return; }
    body.innerHTML = `<div class="muted">감시자 ${sv.running ? '<span class="ok">실행 중</span>' : '<span class="error">멈춤</span>'} · 갱신 ${since(sv.updated_at)} 전</div>` + table([
      { label: "프로그램", html: c => `<span class="dot ${c.running ? "ok" : "err"}"></span> <b>${esc(c.name)}</b>` },
      { label: "실행 시간", num: true, html: c => c.running ? since(c.started_at) : "-" },
      { label: "시작", html: c => `<span class="mono">${dt(c.started_at)}</span>` },
      { label: "재시작", num: true, html: c => c.restarts ? `<span class="warn">${c.restarts}</span>` : "0" },
      { label: "마지막 종료코드", num: true, html: c => c.last_exit ?? "-" },
    ], sv.children);
  },
};

function symLink(symbol, name, code) {
  return `<a href="#" class="sym" data-sym="${esc(symbol)}">${esc(name || symbol)}</a> <span class="muted">${esc(code)}</span>`;
}
function bindSymLinks(body) {
  body.querySelectorAll("a.sym").forEach(a => a.addEventListener("click", e => {
    e.preventDefault(); e.stopPropagation(); setSymbol(a.dataset.sym);
  }));
}

W.chart = {
  controls(card, spec, refresh) {
    const f = document.createElement("div");
    f.className = "filters";
    const ivs = [["1m", "1분"], ["5m", "5분"], ["15m", "15분"], ["60m", "60분"], ["1d", "일"]];
    const own = !currentPage().vars?.includes("symbol");
    f.innerHTML = (own ? `<input data-sym placeholder="종목 (KR:005930)" size="14">` : "") +
      `<span class="chips">${ivs.map(([v, t]) => `<span class="chip btn" data-iv="${v}">${t}</span>`).join("")}</span>
       <span class="chips" data-traded></span>`;
    const mark = () => {
      const cur = spec.live.interval || spec.params.interval || "1m";
      f.querySelectorAll("[data-iv]").forEach(c => c.classList.toggle("on", c.dataset.iv === cur));
    };
    f.querySelectorAll("[data-iv]").forEach(c => c.addEventListener("click", () => { spec.live.interval = c.dataset.iv; mark(); refresh(); }));
    const input = f.querySelector("[data-sym]");
    if (input) {
      input.value = spec.params.symbol || "";
      input.addEventListener("change", () => { spec.live.symbol = input.value.trim().toUpperCase(); refresh(); });
    }
    spec.pick = sym => {
      if (own) { spec.live.symbol = sym; if (input) input.value = sym; refresh(); } else setVar("symbol", sym);
    };
    mark();
    card.insertBefore(f, card.querySelector(".card-body"));
  },
  render(body, full, spec, card) {
    // 앱별 표시 켜고 끄기 (범례의 앱 칩). 데이터는 그대로 두고 다시 그리기만 한다
    spec.hide = spec.hide || new Set();
    const appList = [];
    full.markers.concat(full.positions).forEach(m => { if (!appList.some(a => a.id === m.sub_id)) appList.push({ id: m.sub_id, name: m.sub }); });
    const d = { ...full, markers: full.markers.filter(m => !spec.hide.has(m.sub_id)), positions: full.positions.filter(p => !spec.hide.has(p.sub_id)) };
    const traded = card.querySelector("[data-traded]");
    if (traded) {
      traded.innerHTML = d.symbols.slice(0, 12).map(s => `<span class="chip btn ${s.symbol === d.symbol ? "on" : ""}" data-pick="${esc(s.symbol)}" title="${esc(s.symbol)} 체결 ${s.fills}건">${esc(s.name || s.symbol)}</span>`).join("");
      traded.querySelectorAll("[data-pick]").forEach(c => c.addEventListener("click", () => spec.pick(c.dataset.pick)));
    }
    if (!d.symbol) { body.innerHTML = '<div class="empty">종목을 고르세요 (최근 이틀 체결 종목이 없음)</div>'; return; }
    const head = `<div class="row chart-head"><b>${esc(d.name || d.symbol)}</b><span class="muted">${esc(d.symbol)}</span>
      ${d.last ? `<span>현재 ${esc(fmt(d.last, d.currency === "USD" ? 2 : 0))}</span>` : ""}
      <span class="muted">${esc(d.interval === "1d" ? "일봉" : d.date + " " + d.interval)}</span>
      <span class="chart-info mono muted"></span></div>`;
    const c = candleChart(d, { width: widthOf(body), multiApp: appList.length > 1 });
    if (!c) {
      body.innerHTML = head + `<div class="empty">${d.error ? `<span class="error">${esc(d.error)}</span>` : "봉 없음 (휴장이거나 장 시작 전)"}</div>`;
      return;
    }
    const fills = table([
      { label: "시각", html: m => `<span class="mono">${hms(m.ts)}</span>` },
      { label: "앱", html: m => esc(m.sub) },
      { label: "구분", html: m => SIDE_KO[m.side] },
      { label: "수량", num: true, html: m => fmt(m.qty) },
      { label: "가격", num: true, html: m => fmt(m.price) },
      { label: "실현손익", num: true, html: m => m.side === "sell" ? signed(m.pnl, d.currency) : "" },
    ], d.markers.slice().reverse(), { empty: "이 구간 체결 없음", rowAttrs: m => m.order_id ? `class="click" data-order="${m.order_id}"` : "" });
    body.innerHTML = head + (d.error ? `<div class="error">${esc(d.error)}</div>` : "") + c.html +
      `<div class="legend">${c.multi ? "<span>▲ 매수</span><span>▼ 매도</span><span>--- 평균단가</span>" : '<span class="up">▲ 매수</span><span class="down">▼ 매도</span><span class="warn">--- 평균단가</span>'}
        <span>체결 ${d.markers.length}건</span>${appList.length > 1 ? `<span class="chips">${appList.map(a =>
          `<span class="chip btn ${spec.hide.has(a.id) ? "" : "on"}" data-app="${a.id}" title="눌러서 이 앱 표시 켜기/끄기"><i style="background:${c.multi ? subColor(a.id) : "var(--muted)"}"></i>${esc(a.name)}</span>`).join("")}</span>` : ""}</div>
       <details style="margin-top:6px"><summary>체결 목록</summary>${fills}</details>`;
    body.querySelectorAll("[data-order]").forEach(el => el.addEventListener("click", () => { if (el.dataset.order) openOrder(el.dataset.order); }));
    body.querySelectorAll("[data-app]").forEach(el => el.addEventListener("click", () => {
      const id = Number(el.dataset.app);
      if (spec.hide.has(id)) spec.hide.delete(id); else spec.hide.add(id);
      W.chart.render(body, full, spec, card);
    }));
    const svg = body.querySelector("svg"), info = body.querySelector(".chart-info"), xh = svg.querySelector(".xhair");
    const fy = v => fmt(v, d.currency === "USD" ? 2 : 0);
    svg.querySelector(".hover").addEventListener("mousemove", e => {
      const box = svg.getBoundingClientRect();
      const x = (e.clientX - box.left) * (svg.viewBox.baseVal.width / box.width);
      const i = Math.max(0, Math.min(c.bars.length - 1, Math.floor((x - c.L) / c.slot)));
      const b = c.bars[i];
      xh.setAttribute("x1", c.X(i)); xh.setAttribute("x2", c.X(i)); xh.setAttribute("visibility", "visible");
      const chg = i > 0 ? (b.c - c.bars[i - 1].c) / c.bars[i - 1].c : null;
      info.innerHTML = `${c.daily ? b.iso.slice(0, 10) : b.iso.slice(11, 16)} 시 ${fy(b.o)} 고 ${fy(b.h)} 저 ${fy(b.l)} 종 ${fy(b.c)}` +
        (chg !== null ? ` <span class="${chg > 0 ? "up" : chg < 0 ? "down" : ""}">${chg > 0 ? "+" : ""}${(chg * 100).toFixed(2)}%</span>` : "") + ` 량 ${fmt(b.v, 0)}`;
    });
    svg.querySelector(".hover").addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); info.textContent = ""; });
  },
};

W.symbols = {
  render(body, d) {
    body.classList.add("flush");
    const many = new Set(d.rows.map(r => r.sub_id)).size > 1;
    const cols = [
      { label: "종목", html: r => symLink(r.symbol, r.name, "") },
      { label: "매수/매도", num: true, html: r => `${fmt(r.buy_qty)} / ${fmt(r.sell_qty)}` },
      { label: "보유", num: true, html: r => r.held ? fmt(r.held) : '<span class="muted">0</span>' },
      { label: "실현손익", num: true, html: r => signed(r.pnl, r.currency) },
      { label: "승/패", num: true, html: r => `${r.wins}/${r.losses}` },
      { label: "비용", num: true, html: r => money(r.fee, r.currency) },
      { label: "마지막", html: r => `<span class="mono">${d.from === d.to ? hms(r.last) : dt(r.last)}</span>` },
    ];
    if (many) cols.unshift({ label: "앱", html: r => esc(r.sub) });
    body.innerHTML = `<div class="muted" style="padding:6px 12px">${esc(d.from === d.to ? d.to : d.from + " ~ " + d.to)} · 실현손익은 수수료, 세금 차감</div>` +
      table(cols, d.rows, { tall: true, empty: "체결 없음" });
    bindSymLinks(body);
  },
};

const ENDPOINT_KO = {
  rankings: "순위", flows: "수급", quote: "현재가", orderbook: "호가", candles: "캔들", history: "과거 봉", ticks: "체결 기록",
  symbols: "종목", fx: "환율", calendar: "휴장일", status: "시장 상태", ranking_kinds: "순위 종류", flow_kinds: "수급 종류",
  history_subscriptions: "과거 봉 구독", history_subscribe: "과거 봉 구독", history_unsubscribe: "과거 봉 구독 해제",
  me: "내 정보", balance: "잔고", positions: "보유", ledger: "원장", pnl: "손익", place: "주문", order_list: "주문 조회",
  order_get: "주문 조회", order_cancel: "취소", order_amend: "정정", cancel_all: "전체 취소", fill_list: "체결 조회",
};
// 서버 캐시나 실시간 수신값으로 답할 수 있는 시세 조회 (주문, 계좌 조회는 해당 없음)
const CACHEABLE = new Set(["quote", "orderbook", "candles", "history", "rankings", "flows", "ticks", "fx", "calendar", "symbols", "status"]);
function channelKo(c) {
  const [kind, ...rest] = c.split(":");
  const k = { tick: "체결", orderbook: "호가", "candle.1m": "1분봉" }[kind] || kind;
  return `${k} ${rest.join(":")}`;
}

W.usage = {
  render(body, d) {
    if (!d.rows.length) {
      body.innerHTML = '<div class="empty">최근 1시간 앱 요청과 실시간 연결 없음</div>';
      return;
    }
    const sum = table([
      { label: "앱", html: r => `<span class="dot" style="background:${subColor(r.sub_id)}"></span> <b>${esc(r.name)}</b>` },
      { label: "요청 (1시간)", num: true, html: r => fmt(r.requests) },
      { label: "분당", num: true, html: r => fmt(r.requests / 60, 1) },
      { label: "KIS 호출", num: true, html: r => fmt(r.kis_calls) },
      { label: "오류", num: true, html: r => r.errors ? `<span class="error">${r.errors}</span>` : "0" },
      { label: "평균 응답", num: true, html: r => r.elapsed_avg_ms === null ? "-" : `${fmt(r.elapsed_avg_ms, 0)}ms` },
      { label: "마지막 요청", num: true, html: r => r.last_at ? `${since(r.last_at)} 전` : "-" },
      { label: "WS 연결", num: true, html: r => r.ws_connections ? `${r.ws_connections} <span class="muted">(${since(r.ws_since)})</span>` : '<span class="muted">0</span>' },
      { label: "실시간 구독", num: true, html: r => fmt(r.channels.length) },
    ], d.rows);
    const perApp = {};
    d.rows.forEach(r => { perApp[r.name] = r.per_minute; });
    const chart = stackedBars(perApp, { width: widthOf(body), colors: d.rows.map(r => subColor(r.sub_id)), unit: "요청" });
    const detail = d.rows.map(r => {
      const eps = table([
        { label: "요청 종류", html: e => `${esc(ENDPOINT_KO[e.name] || e.name)} <span class="muted mono">${esc(e.name)}</span>` },
        { label: "요청", num: true, html: e => fmt(e.requests) },
        { label: "KIS 호출", num: true, html: e => fmt(e.kis_calls) },
        { label: "캐시 응답", num: true, html: e => e.requests && CACHEABLE.has(e.name) ? pct(Math.max(0, 1 - e.kis_calls / e.requests), 0) : "-" },
        { label: "오류", num: true, html: e => e.errors ? `<span class="error">${e.errors}</span>` : "0" },
      ], r.endpoints, { empty: "최근 1시간 요청 없음" });
      const ch = r.channels.length
        ? `<div class="chips" style="margin-top:6px">${r.channels.map(c => `<span class="chip">${esc(channelKo(c))}</span>`).join("")}</div>`
        : '<div class="muted" style="margin-top:6px">실시간 구독 없음</div>';
      return `<details style="margin-top:8px"><summary><b>${esc(r.name)}</b> 요청 종류 ${r.endpoints.length}개 · 구독 ${r.channels.length}개</summary>${eps}${ch}</details>`;
    }).join("");
    const server = d.kis_total_60m !== undefined
      ? `<div class="muted" style="margin-top:8px">KIS 호출 1시간 합계 ${fmt(d.kis_total_60m)}건 중 앱 요청 몫 ${fmt(d.kis_total_60m - d.kis_server_60m)}건,
          서버 자체 작업(대사, 동기화, 일정 작업, 대시보드 차트) ${fmt(d.kis_server_60m)}건</div>` : "";
    body.innerHTML = sum + `<div style="margin-top:10px">${chart}</div>` + server + detail +
      '<div class="muted" style="margin-top:6px">서버 재시작 때 비워짐. KIS 호출은 그 요청 처리 중 실제로 나간 것만 (캐시 응답은 0)</div>';
  },
};

// ───────────── 주문 상세 ─────────────
async function openOrder(id) {
  openModal(`주문 #${id}`, '<div class="muted">불러오는 중</div>');
  try {
    const d = await api(`/orders/${id}`);
    const o = d.order;
    const kv = [
      ["앱", `${esc(o.sub)} #${o.sub_id} (${esc(o.broker)})`], ["종목", `${esc(o.name)} ${esc(o.symbol)}`],
      ["구분", `${SIDE_KO[o.side]} ${o.type === "market" ? "시장가" : "지정가"} ${o.route ? esc(o.route) : ""}`],
      ["가격 / 수량", `${o.price ? fmt(o.price) : "-"} / ${fmt(o.qty)}`], ["체결", o.filled ? `${fmt(o.filled)} @ ${fmt(o.avg_price)}` : "-"],
      ["상태", badge(o.status) + (o.reason ? ` <span class="error">${esc(o.reason)}</span>` : "")],
      ["접수까지", o.accept_ms === null ? "-" : `${(o.accept_ms / 1000).toFixed(2)}초`],
      ["client_order_id", `<span class="mono">${esc(o.client_id)}</span>`], ["원주문", o.orig_id ? `#${o.orig_id}` : "-"],
    ].map(([k, v]) => `<div class="k">${esc(k)}</div><div>${v}</div>`).join("");
    const t0 = new Date(o.ts).getTime();
    const ev = table([
      { label: "시각", html: e => `<span class="mono">${hms(e.ts)}</span>` },
      { label: "+초", num: true, html: e => ((new Date(e.ts).getTime() - t0) / 1000).toFixed(2) },
      { label: "이벤트", html: e => e.type === "FILL" ? `체결 ${fmt(e.qty)} @ ${fmt(e.price)}` : e.type === "ORDER_UNKNOWN" ? badge("UNKNOWN") : badge(e.status) },
      { label: "사유", cls: "wrap", html: e => esc(e.reason || "") },
    ], d.events, { empty: "이벤트 없음" });
    const fills = table([
      { label: "체결시각", html: f => `<span class="mono">${hms(f.filled_at)}</span>` },
      { label: "수량", num: true, html: f => fmt(f.qty) }, { label: "가격", num: true, html: f => fmt(f.price) },
      { label: "수수료", num: true, html: f => money(f.fee, f.currency) }, { label: "세금", num: true, html: f => money(f.tax, f.currency) },
      { label: "실현손익", num: true, html: f => signed(f.realized_pnl, f.currency) },
    ], d.fills, { empty: "체결 없음" });
    openModal(`주문 #${id}`, `<div class="kv">${kv}</div><div><b>이력</b>${ev}</div><div><b>체결</b>${fills}</div>`);
  } catch (e) {
    openModal(`주문 #${id}`, `<div class="error">${esc(e.message)}</div>`);
  }
}

function openModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }

// ───────────── 레이아웃 편집 ─────────────
function openEditor() {
  const catalog = S.me.widgets.map(w => `<tr><td class="mono"><b>${esc(w.type)}</b></td><td>${esc(w.title)}</td>
    <td class="wrap muted">${Object.entries(w.params).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>") || "-"}</td></tr>`).join("");
  openModal("레이아웃", `
    <div class="muted">pages 배열이 탭 순서입니다. 위젯의 w 는 12칸 중 차지할 폭, params 는 위젯 조건(문자열), title 은 제목 바꾸기(선택).
      ${S.custom ? "지금은 저장한 레이아웃을 쓰고 있습니다." : "지금은 기본 레이아웃입니다."}</div>
    <textarea id="layout-json" spellcheck="false">${esc(JSON.stringify(S.layout, null, 2))}</textarea>
    <div class="row"><button id="layout-save">저장</button><button id="layout-reset" class="ghost">기본값으로</button>
      <span id="layout-msg" class="error"></span></div>
    <div><b>쓸 수 있는 위젯</b><div class="scroll"><table><thead><tr><th>type</th><th>이름</th><th>params</th></tr></thead><tbody>${catalog}</tbody></table></div></div>`);
  $("#layout-save").onclick = async () => {
    let parsed;
    try { parsed = JSON.parse($("#layout-json").value); } catch (e) { $("#layout-msg").textContent = "JSON 형식 오류: " + e.message; return; }
    try { applyLayout(await api("/layout", { method: "PUT", body: JSON.stringify(parsed) })); closeModal(); }
    catch (e) { $("#layout-msg").textContent = e.message; }
  };
  $("#layout-reset").onclick = async () => {
    try { applyLayout(await api("/layout", { method: "DELETE" })); closeModal(); }
    catch (e) { $("#layout-msg").textContent = e.message; }
  };
}

// ───────────── 화면 조립 ─────────────
function stopTimers() { S.timers.forEach(t => clearTimeout(t)); S.timers = []; }

function applyLayout(d) {
  S.layout = d.layout; S.custom = d.custom;
  renderTabs();
  renderPage();
}

function currentPageId() {
  const h = decodeURIComponent(location.hash.slice(1));
  return S.layout.pages.some(p => p.id === h) ? h : S.layout.pages[0].id;
}

function renderTabs() {
  const cur = currentPageId();
  $("#tabs").innerHTML = S.layout.pages.map(p => `<a href="#${encodeURIComponent(p.id)}" class="${p.id === cur ? "active" : ""}">${esc(p.title)}</a>`).join("");
}

function currentPage() { return S.layout.pages.find(p => p.id === currentPageId()); }

function pageVars() {
  const page = currentPage();
  const vals = S.vars[page.id] || {};
  const out = {};
  (page.vars || []).forEach(k => { if (vals[k]) out[k] = vals[k]; });
  return out;
}

function saveVars() { try { localStorage.setItem("cs_ui_vars", JSON.stringify(S.vars)); } catch (_) { /* 저장 불가 */ } }
function loadVars() { try { S.vars = JSON.parse(localStorage.getItem("cs_ui_vars") || "{}") || {}; } catch (_) { S.vars = {}; } }

function setVar(k, v) {
  const page = currentPage();
  (S.vars[page.id] = S.vars[page.id] || {})[k] = v;
  saveVars();
  renderBar(page);
  S.widgets.forEach(r => r());
}

// 종목 링크: 이 페이지에 종목 선택 칸이 있으면 거기에, 없으면 종목 칸이 있는 첫 페이지로 이동
function setSymbol(sym) {
  const page = currentPage();
  if ((page.vars || []).includes("symbol")) { setVar("symbol", sym); return; }
  const target = S.layout.pages.find(p => (p.vars || []).includes("symbol"));
  if (!target) return;
  (S.vars[target.id] = S.vars[target.id] || {}).symbol = sym;
  saveVars();
  location.hash = encodeURIComponent(target.id);
}

function renderBar(page) {
  const bar = $("#pagebar");
  const vars = page.vars || [];
  bar.hidden = !vars.length;
  if (!vars.length) { bar.innerHTML = ""; return; }
  const v = S.vars[page.id] || {};
  const parts = vars.map(k => {
    if (k === "sub") return `<label>앱 <select data-var="sub"><option value="">전체</option>${S.me.subs.map(s =>
      `<option value="${s.id}" ${String(v.sub) === String(s.id) ? "selected" : ""}>${esc(s.type === "system" ? "미배정(" + s.broker + ")" : s.name)} #${s.id}</option>`).join("")}</select></label>`;
    if (k === "date") return `<label>날짜 <input type="date" data-var="date" value="${esc(v.date || "")}"></label>`;
    if (k === "symbol") return `<label>종목 <input data-var="symbol" placeholder="비우면 최근 체결" size="14" value="${esc(v.symbol || "")}"></label>`;
    return "";
  }).join("");
  const any = vars.some(k => v[k]);
  bar.innerHTML = parts + (any ? '<button class="ghost" data-clear>초기화</button>' : "");
  bar.querySelectorAll("[data-var]").forEach(el => el.addEventListener("change", () => {
    setVar(el.dataset.var, el.dataset.var === "symbol" ? el.value.trim().toUpperCase() : el.value);
  }));
  const clear = bar.querySelector("[data-clear]");
  if (clear) clear.onclick = () => { S.vars[page.id] = {}; saveVars(); renderPage(); };
}

function renderPage() {
  stopTimers();
  S.widgets = [];
  const page = currentPage();
  renderBar(page);
  const root = $("#page");
  root.innerHTML = "";
  page.widgets.forEach(spec => mountWidget(root, { ...spec, params: spec.params || {}, live: {} }));
}


function mountWidget(root, spec) {
  const meta = S.catalog[spec.type];
  const impl = W[spec.type];
  const card = document.createElement("section");
  card.className = "card";
  card.style.gridColumn = `span ${Math.max(1, Math.min(12, spec.w || 12))}`;
  card.innerHTML = `<div class="card-head"><h3>${esc(spec.title || (meta ? meta.title : spec.type))}</h3><span class="stamp"></span></div><div class="card-body"></div>`;
  root.appendChild(card);
  const body = card.querySelector(".card-body");
  const stamp = card.querySelector(".stamp");
  if (!meta || !impl) { body.innerHTML = `<div class="error">알 수 없는 위젯 ${esc(spec.type)}</div>`; return; }

  let timer = null, busy = false, again = false;
  const refresh = async () => {
    if (busy) { again = true; return; }   // 갱신 중에 조건이 바뀌면 끝난 뒤 한 번 더
    busy = true;
    again = false;
    clearTimeout(timer);
    try {
      const q = new URLSearchParams();
      Object.entries({ ...spec.params, ...pageVars(), ...spec.live }).forEach(([k, v]) => { if (v !== "" && v !== null && v !== undefined) q.set(k, v); });
      const data = await api(`/w/${spec.type}${q.toString() ? "?" + q : ""}`);
      impl.render(body, data, spec, card);
      stamp.textContent = new Date().toTimeString().slice(0, 8);
      stamp.classList.remove("stale");
    } catch (e) {
      stamp.textContent = "갱신 실패";
      stamp.classList.add("stale");
      stamp.title = e.message;
      if (!body.innerHTML) body.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    } finally {
      busy = false;
      if (again && S.me) { refresh(); return; }
      if (S.me) {
        timer = setTimeout(tick, Math.max(3, meta.refresh) * 1000);
        S.timers.push(timer);
      }
    }
  };
  const tick = () => { if (document.hidden) { timer = setTimeout(tick, 2000); S.timers.push(timer); } else refresh(); };
  if (impl.controls) impl.controls(card, spec, refresh);
  S.widgets.push(refresh);
  refresh();
}

// ───────────── 시작 ─────────────
function showLogin() {
  stopTimers();
  S.me = null;
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-key").value = "";
  $("#login-key").focus();
}

async function boot() {
  try {
    S.me = await api("/me");
  } catch (_) { return; }
  S.catalog = Object.fromEntries(S.me.widgets.map(w => [w.type, w]));
  loadVars();
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#who").textContent = S.me.role === "admin" ? "관리자" : S.me.name;
  applyLayout(await api("/layout"));
}

$("#login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const err = $("#login-error");
  err.hidden = true;
  const btn = ev.target.querySelector("button");
  btn.disabled = true;
  try {
    await api("/login", { method: "POST", body: JSON.stringify({ key: $("#login-key").value.trim() }) });
    $("#login-key").value = "";
    await boot();
  } catch (e) {
    err.textContent = e.message.includes("로그인") ? "키가 올바르지 않습니다" : e.message;
    err.hidden = false;
    $("#login").hidden = false;
  } finally { btn.disabled = false; }
});
$("#btn-logout").onclick = async () => { try { await api("/logout", { method: "POST" }); } finally { showLogin(); } };
$("#btn-edit").onclick = openEditor;
$("#modal-close").onclick = closeModal;
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
window.addEventListener("hashchange", () => { if (S.layout) { renderTabs(); renderPage(); } });
setInterval(() => { $("#clock").textContent = new Date().toLocaleString("ko-KR", { hour12: false }); }, 1000);
boot();
