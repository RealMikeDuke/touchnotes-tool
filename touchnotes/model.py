"""中间表示：规范化 JSON —— 导入/导出的公共接口。

```json
{
  "format": "touchnotes/1",
  "note":  {"file_id": "...", "name": "...", "page_count": 2},
  "pages": [
    {"id": "note_page...", "index": 0, "viewport": {"angle":0,"offsetX":0,...},
     "objects": [
       {"kind": "stroke", "color": "#RRGGBB", "alpha": 255, "width": 8.0,
        "points": [[x, y, pressure], ...], "identity": "<24B hex，原样保留可字节级重建>"},
       {"kind": "polygon|circle|wave|bezier|pen", "tool": "ShapeTool",
        "color": "#RRGGBB", "alpha": 255, "width": 6.6176467,
        "closed": true, "arrow": 0, "dotted": false, "square": false, "fill": null,
        "points": [[x, y], ...],
        "attrs": { ...该类型剩余字段原样保留，重建时写回... }}
     ]}
  ],
  "source": {"dir": "...", "pages": ["..."]}
}
```

`points` 的点数即 App 索引块里的 `pointSize`（重建时自动写入）；
`attrs` 保留未解读字段（`cardUuid` 等），保证「读出来 → 写回去」不丢东西。
"""
import json
import os

from . import container as C
from . import records as R

FORMAT = "touchnotes/1"
_HIDDEN = ("color", "width", "pointSize")


def color_hex(c):
    """App 的 ARGB 整数 → '#RRGGBB'。"""
    return "#%06X" % (int(c) & 0xFFFFFF)


def color_alpha(c):
    return (int(c) >> 24) & 0xFF


def hex_color(s, alpha=255):
    s = s.lstrip("#")
    if len(s) == 8:
        return (int(s[:2], 16) << 24) | int(s[2:], 16)
    return (int(alpha) << 24) | int(s, 16)


def page_model(page):
    objs = []
    for r in page["records"]:
        if r["kind"] == "stroke":
            objs.append({"kind": "stroke", "color": color_hex(r["color"]),
                         "alpha": color_alpha(r["color"]), "width": round(r["width"], 6),
                         "points": [[round(x, 3), round(y, 3), round(p, 4)] for x, y, p in r["points"]],
                         "identity": r["raw"][-24:].hex()})
        else:
            m = r["json"] or {}
            objs.append({"kind": r["kind"], "tool": r["tool"], "color": color_hex(m.get("color", 0)),
                         "alpha": m.get("alpha", 255), "width": m.get("width"),
                         "closed": m.get("closePath", True), "arrow": m.get("arrow", 0),
                         "dotted": m.get("dotted", False), "square": m.get("square", False),
                         "fill": color_hex(m["fillColor"]) if m.get("fillColor") else None,
                         "points": [[round(x, 3), round(y, 3)] for x, y in r["points"]],
                         "attrs": {k: v for k, v in m.items() if k not in _HIDDEN}})
    return {"id": page["props"].get("id"), "index": page["props"].get("index"),
            "viewport": page["head"], "objects": objs}


def note_model(file_id, root=None):
    d = C.notebook_dir(root)
    ncc = C.read_ncc(os.path.join(d, file_id + ".ncc"))
    md = ncc["blocks"][0]
    paths = C.page_paths(file_id, root)
    pages = [C.read_page(p) for p in paths]
    return {"format": FORMAT,
            "note": {"file_id": file_id, "name": md.get("name"), "page_count": len(pages)},
            "pages": [page_model(p) for p in pages],
            "source": {"dir": d, "pages": paths}}


def dump(file_id, out=None, root=None, indent=None, root_sel="note"):
    m = note_model(file_id, root)
    if out:
        with open(out, "w") as f:
            json.dump(m, f, ensure_ascii=False, indent=indent)
        return out
    return json.dumps(m, ensure_ascii=False, indent=indent)
