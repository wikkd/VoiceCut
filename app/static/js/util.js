// VoiceCut 前端 — 通用工具（无应用状态依赖）
// ── 领域类型（与后端 app/web/*.py 的 JSON 契约对应）──
// 角色池角色（对应 /api/projects/<id>/characters）
/** @typedef {{id: string, name: string, color: string, speakerLabels?: string[], created?: string, embedding?: number[]|null}} Character */
// 说话人分段
/** @typedef {{start: number, end: number, label: string|null}} SpeakerSeg */
// 训练片段（对应 /api/items/<id>/project 的 segments）
/** @typedef {{id: string, start: number, end: number, text: string, language: "JP"|"ZH"|"EN", speakerLabel?: string|null, characterId?: string|null, mixed?: boolean, locked?: boolean, q?: number|null}} Segment */
// 素材（对应 /api/items）
/** @typedef {{id: string, name: string, kind: string, duration: number, sample_rate?: number, video_url?: string|null, peaks?: number[][]|null, peaks_url?: string, project_id?: string}} Item */
// 实时字幕行
/** @typedef {{start: number, end: number, text: string}} SubLine */

/** @param {string} s @returns {HTMLElement|null} */
export const $ = (s) => /** @type {HTMLElement|null} */ (document.querySelector(s));
export const $$ = (s) => Array.from(document.querySelectorAll(s));

export const LS_PROJECT = "vc.project.v1";   // 记住上次选中的项目
export const SEG_MIN = 1.0, SEG_MAX = 15.0;
export const SEEK_STEP = 5, SEEK_FAST = 15, NUDGE_STEP = 0.5, VOL_STEP = 0.05; // 快退快进秒数 / 选区微调步长(s) / 音量步进(5%)

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

/** @param {string} s @returns {string} */
export function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
/** @param {string} s @param {number} [n] @returns {string} */
export function shortName(s, n = 10) { s = s || ""; return s.length > n ? s.slice(0, n) + "…" : s; }

/** @param {string} url @param {RequestInit} [opts] @returns {Promise<any>} */
export async function api(url, opts) {
  const r = await fetch(url, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
  return j;
}

/** 事件委托：取事件目标元素（document/容器级监听器的 e.target 类型是 EventTarget，需收窄）。
 *  委托目标可能是行内 input/select/textarea，故附上 Partial 输入控件属性（value/checked 等）。 */
/** @typedef {HTMLElement & Partial<HTMLInputElement & HTMLSelectElement & HTMLTextAreaElement>} DelegatedEl */
/** @param {Event} e @returns {DelegatedEl} */
export const evtEl = (e) => /** @type {DelegatedEl} */ (e.target);
/** closest 到具体元素（Element.closest 返回 Element，委托行操作需要 HTMLElement）@param {Element} el @param {string} sel @returns {HTMLElement|null} */
export const closestEl = (el, sel) => /** @type {HTMLElement|null} */ (el.closest(sel));
