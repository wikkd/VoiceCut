"""前端模块接线静态审计：检查 app.js 启动块里各 createXxx(ctx) 是否
在「创建时」就取值了「更晚创建」的模块实例（会永久捕获 null，运行到才炸）。

用法：python scripts/ctx_order_audit.py   （无输出异常即为通过）
"""
import io
import re

app = io.open("app/static/js/app.js", encoding="utf-8").read()
INSTS = ["store", "projects", "tasks", "segments", "pool", "subtitles", "training", "io", "waveform"]

order = {}
for m in re.finditer(r"^  (\w+) = create(\w+)\(\{", app, re.M):
    order[m.group(1)] = m.start()
print("创建顺序:", " → ".join(sorted(order, key=order.get)))


def split_top(body):
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


bad = []
for m in re.finditer(r"^  (\w+) = create(\w+)\(\{", app, re.M):
    me = m.group(1)
    i = m.end(); depth = 1
    while depth:
        depth += (app[i] == "{") - (app[i] == "}")
        i += 1
    body = app[m.end():i - 1]
    for part in split_top(body):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        val = val.strip()
        if "=>" in val or "function" in val or "(" in val:
            continue  # 闭包/调用：运行时才求值，安全
        inst = re.match(r"^(\w+)(?:\.\w+)?$", val)
        if not inst:
            continue
        tok = inst.group(1)
        if tok in INSTS and tok != me and order.get(tok, -1) > order[me]:
            bad.append((me, key.strip(), val))

print("❌ 创建时即取值且更晚创建的实例引用:", bad if bad else "无（跨模块引用全部为闭包/更早实例）")
