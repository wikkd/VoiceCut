"""筛选「适合 CSS mask 着色」的 HarmonyOS 图标。

背景：图标 SVG 是 Sketch 导出结构 —— <defs><path id="path-N"/></defs> + <mask fill="white">
+ <use fill="#000000">。走 CSS mask 时只有 alpha 通道参与着色，所以：
- 纯单色（只有 black/white/none 作底）→ 安全，剪影即图形
- 含品牌色（#0A59F7 / #FFA800 / #E84026 ...）→ 剪影会塌掉（如「蓝色圆+白加号」变成实心圆）→ 淘汰

输出：mono / colored 两份清单。
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ZIP = Path(r"D:\harmony资源包\HarmonyOS_Icons.zip")

# 允许出现在单色图标里的填充（mask 脚手架）
BENIGN = {
    "none", "white", "#FFFFFF", "#fff", "#FFF",
    "#000000", "#000", "black",
}
# Sketch 把 mask 画成 white 底，把图形画成黑 —— 这些都不影响剪影。
# 但同一文件里若同时出现多个「非脚手架」色，或出现品牌色，判定为彩色图标。

HEX = re.compile(r'^\s*(#[0-9a-fA-F]{3,8})\s*$')


def classify(svg: str) -> tuple[str, list[str]]:
    """返回 ('mono'|'colored', 出现的非脚手架色)。"""
    fills = set(re.findall(r'fill="([^"]*)"', svg))
    # 去掉 url(#...) 与 inherit / currentColor
    weird = {f for f in fills
             if f not in BENIGN and not f.startswith("url(") and f not in ("inherit", "currentColor")}
    brand = set()
    for f in weird:
        m = HEX.match(f)
        if m:
            v = m.group(1).lstrip("#").lower()
            if v in ("000", "000000", "fff", "ffffff"):
                continue
            brand.add(f)
        else:
            brand.add(f)     # 命名色（red/blue/#B4C4D0 之类）
    # url(#...) 引用渐变/mask 也算彩色风险
    grads = {f for f in fills if f.startswith("url(")}
    colored = bool(brand) or bool(grads)
    return ("colored" if colored else "mono"), sorted(brand | grads)


def main() -> None:
    wanted = sys.argv[1:] or None
    with zipfile.ZipFile(ZIP) as z:
        names = sorted(z.namelist())
        mono, colored = [], []
        for n in names:
            if wanted and Path(n).stem not in wanted:
                continue
            kind, colors = classify(z.read(n).decode("utf-8"))
            (mono if kind == "mono" else colored).append((Path(n).stem, colors))
    print(f"=== 单色（可 mask 着色）：{len(mono)} 个 ===")
    for name, _ in mono:
        print(" ", name)
    print(f"\n=== 彩色（剪影会塌，需用 <img> 保留原色或淘汰）：{len(colored)} 个 ===")
    for name, colors in colored:
        print(f"  {name}  {colors}")


if __name__ == "__main__":
    main()
