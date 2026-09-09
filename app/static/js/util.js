// VoiceCut 前端 — 通用工具（无应用状态依赖）
export const $ = (s) => document.querySelector(s);
export const $$ = (s) => Array.from(document.querySelectorAll(s));

export const SEG_MIN = 1.0, SEG_MAX = 15.0;
export const SEEK_STEP = 5, SEEK_FAST = 15, VOL_STEP = 0.05; // 快退快进秒数 / 音量步进(5%)

// 角色调色板（深色 UI 下的高对比色相）
export const CHAR_PALETTE = [
  "#e5484d", "#f76808", "#f5d90a", "#46a758", "#3e63dd",
  "#8e4ec6", "#12a594", "#e93d82", "#00a2c7", "#ffb224",
];

export const fmtT = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
};
export const fmtSel = (sel) => sel ? `${fmtT(sel.start)} ~ ${fmtT(sel.end)}` : "—";
export const fmtDur = (d) => `${d.toFixed(1)}s`;
export const clampN = (v, a, b) => Math.max(a, Math.min(b, v));

export function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
export function shortName(s, n = 10) { s = s || ""; return s.length > n ? s.slice(0, n) + "…" : s; }

export async function api(url, opts) {
  const r = await fetch(url, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
  return j;
}
