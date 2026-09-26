"""HarmonyOS 图标一致性自检（零依赖，pytest 之外的独立门）。

为什么需要它：`.ico` 的图形来自 `-webkit-mask: var(--ico)`，而 `--ico` 只在
`.ico-<name>` 里赋值。一旦某个类名在 HTML/JS 里用了、CSS 里却没定义，
`var(--ico)` 失效会让整条 mask 声明作废、`background-color: currentColor`
直接露出来 —— 渲染成 1em 实心方块。这种「像图标又不像图标」的结果肉眼很容易漏掉。

检查四件事（全部 ERROR 级，任一命中即非零退出）：
  1. 用到的类名 ⊆ 定义的类名
  2. 每个 `.ico-<name>` 指向的 SVG 文件真实存在
  3. JS 里通过 ico("<name>") 调用的名字都在 CSS 里有定义
  4. `.ico` 基础类保留空遮罩兜底（防止未定义类名渲染成实心块）

用法：python scripts/check_icons.py [--quiet]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "static"
CSS = APP / "css" / "style.css"
HTML = APP / "index.html"
JS_DIRS = [APP / "js"]
VENDOR = APP / "vendor" / "harmony-icons"

CLS_RE = re.compile(r"ico-[a-z0-9-]+")
# CSS 里 .ico-<name> { --ico: url("...") } —— 允许多条声明/多行
DEF_RE = re.compile(r"\.(ico-[a-z0-9-]+)\s*\{[^}]*?url\(\"([^\"]+)\"\)", re.S)
# JS 里 ico("name") / ico('name') / ico(cond ? "a" : "b") —— 只有「第一个参数」是图标名，
# 第二个参数是追加 class（如 "lg"），所以先切出首参再取其中的字符串字面量。
CALL_RE = re.compile(r"\bico\(([^()]*)\)")
STR_RE = re.compile(r"""["'`]([a-z0-9-]+)["'`]""")
EMPTY_MASK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def js_files() -> list[Path]:
    out: list[Path] = []
    for d in JS_DIRS:
        out.extend(sorted(d.glob("*.js")))
        out.extend(sorted(d.glob("**/*.js")))
    return sorted(set(out))


def main() -> int:
    css = read(CSS)
    html = read(HTML)

    defined: dict[str, str] = {}          # 类名 → url
    for name, url in DEF_RE.findall(css):
        defined[name] = url
    defined_names = set(defined)

    errors: list[str] = []

    # ── 1) 用到的类名（HTML + JS 模板里写死的 class="ico ico-xxx"） ──
    used: dict[str, set[str]] = {}
    for src, text in [("index.html", html)] + [(f.relative_to(ROOT).as_posix(), read(f)) for f in js_files()]:
        for m in CLS_RE.findall(text):
            used.setdefault(m, set()).add(src)

    # 只统计「真的当类名用」的：排除 CSS 里自己的定义行与注释里的举例。
    # HTML/JS 里出现的 ico-xxx 一律算使用（脚本自身文件除外）。
    for name, srcs in sorted(used.items()):
        if name in ("ico-",):
            continue
        if name not in defined_names:
            errors.append(f"[1] 用到但未定义：.{name}  ← {', '.join(sorted(srcs))}（会渲染成 1em 实心方块）")

    # ── 2) 定义指向的 SVG 是否存在 ──
    for name, url in sorted(defined.items()):
        rel = url.split("/vendor/")[-1] if "/vendor/" in url else url
        if not rel.endswith(".svg"):
            continue        # 内联 data: URI 或非 svg，跳过
        if not (VENDOR / Path(rel).name).is_file():
            errors.append(f"[2] 文件缺失：.{name} → {rel}")

    # ── 3) JS ico("name") 调用的名字 ──
    called: dict[str, set[str]] = {}
    for f in js_files():
        for inner in CALL_RE.findall(read(f)):
            first = inner.split(",")[0]          # 首参 = 图标名（第二参是追加 class）
            for m in STR_RE.findall(first):
                called.setdefault("ico-" + m, set()).add(f.relative_to(ROOT).as_posix())
    for name, srcs in sorted(called.items()):
        if name not in defined_names:
            errors.append(f"[3] ico() 调用未定义：.{name}  ← {', '.join(sorted(srcs))}")

    # ── 4) 兜底遮罩仍在 ──
    if EMPTY_MASK not in css:
        errors.append("[4] .ico 缺少空遮罩兜底（未定义类名会渲染成实心块）")

    # ── 5) vendor 目录里是否有谁都没引用的 SVG（仅提示，不算失败） ──
    #    注意：不能只看 .ico-<name> 规则 —— .sort-arrow 这类非 ico 前缀的类也会用
    #    同一批图标，所以直接扫 CSS 里所有指向 vendor/harmony-icons 的 url()。
    referenced = set(re.findall(r"harmony-icons/([a-zA-Z0-9_.-]+\.svg)", css))
    orphans = sorted(p.name for p in VENDOR.glob("*.svg") if p.name not in referenced)

    # ── 报告 ──
    unused = sorted(defined_names - set(used) - set(called))
    quiet = "--quiet" in sys.argv
    if not quiet:
        print(f"已定义 {len(defined_names)} 个类 · HTML/JS 用到 {len(used)} 个 · ico() 调用 {len(called)} 个")
        if unused:
            print(f"已定义但未使用（仅提示）：{', '.join('.' + u for u in unused)}")
        if orphans:
            print(f"vendor 里未被任何类引用（可删）：{', '.join(orphans)}")
    if errors:
        print(f"\n✗ 图标自检失败（{len(errors)} 项）：")
        for e in errors:
            print("  " + e)
        return 1
    print("✓ 图标自检通过：类名、文件、ico() 调用、兜底遮罩全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
