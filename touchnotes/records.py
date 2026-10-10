"""记录层：v1 手写 / v2 形状记录的解析与编码、类型映射。

**类型（形状种类）不在记录字节里**，而是存在页面索引块 `ms[].n` 字段里：

| `ms[].n`     | 含义                                        | 点数  |
|--------------|---------------------------------------------|-------|
| `FunPen`     | 自由手写（每点带压感）                      | N     |
| `ShapeTool`  | 闭合多边形（直线 2 / 矩形 4 / 五角星 10）   | N     |
| `CircleTool` | 圆 = 圆心 + 圆上一点                        | 2     |
| `WaveTool`   | 两点之间的波浪（由 App 生成）               | 2     |
| `BezierTool` | 二次贝塞尔（起点/控制点/终点）              | 3     |
| `Pen`        | 平滑路径对象（密集采样，JSON schema 不同）  | N     |

v1 记录：`u32 255 | f32 width | u32 argb | u32 n | n×(f32 x, y, pressure) | 24B 身份`
v2 记录：`[前缀 ~136/140B][u32 JSON长度][JSON][u32 点数][n×(f32 x, y)]`
"""
import json
import struct

MAGIC_V2 = b"\x01\x33\xf0\xe9"    # v2 记录分隔魔数
MAGIC_PEN = b"\x01\x34\x64\x7c"   # Pen 对象魔数

TOOL2KIND = {"FunPen": "stroke", "ShapeTool": "polygon", "CircleTool": "circle",
             "WaveTool": "wave", "BezierTool": "bezier", "Pen": "pen"}
KIND2TOOL = {v: k for k, v in TOOL2KIND.items()}

COMPACT = dict(ensure_ascii=False, separators=(",", ":"))

MAX_POINTS = 400000   # 解析防护

_b32 = lambda n: struct.pack(">I", n)
_f32 = lambda v: struct.pack(">f", v)
_u32 = lambda b, o: struct.unpack(">I", b[o:o + 4])[0]
_f32r = lambda b, o: struct.unpack(">f", b[o:o + 4])[0]

# ---------------------------------------------------------------- 解析
def parse_v1(rest, o):
    """偏移 o 处如果是 v1 手写记录，返回 (结束偏移, {width, color, points=[(x,y,p)]})。"""
    if o + 16 > len(rest) or _u32(rest, o) != 255:
        return None
    n = _u32(rest, o + 12)
    if not (0 < n <= MAX_POINTS):
        return None
    end = o + 16 + n * 12 + 24
    if end > len(rest):
        return None
    pts = [struct.unpack(">fff", rest[o + 16 + i * 12:o + 28 + i * 12]) for i in range(n)]
    return end, {"width": _f32r(rest, o + 4), "color": _u32(rest, o + 8), "points": pts,
                 "identity": rest[end - 24:end]}

def parse_v2(rest, o):
    """偏移 o 处如果是 v2 形状记录，返回 (结束偏移, {prefix, meta, points=[(x,y)]})。"""
    k = rest.find(b'{"', o, o + 512)
    if k < 0 or k + 4 > len(rest):
        return None
    jl = _u32(rest, k - 4)
    if not (2 < jl < 10 ** 6) or k + jl > len(rest):
        return None
    try:
        meta = json.loads(rest[k:k + jl].decode("utf-8"))
    except Exception:
        return None
    p = k + jl
    if p + 4 > len(rest):
        return None
    n = _u32(rest, p)
    if not (0 < n <= MAX_POINTS):
        return None
    end = p + 4 + n * 8
    if end > len(rest):
        return None
    pts = [struct.unpack(">ff", rest[p + 4 + i * 8:p + 12 + i * 8]) for i in range(n)]
    return end, {"prefix": rest[o:k - 4], "meta": meta, "points": pts}

def scan(rest):
    """贪心混合扫描记录区：v1 与 v2 交替出现也能全部读出。

    两种记录会混在同一页（实测 Minecraft 那页 239 条 = 208 手写 + 31 图形），
    只认一种会把另一种静默丢掉。
    返回 [(起始偏移, 结束偏移, 解析结果)]。
    """
    out, o = [], 0
    while o < len(rest):
        r = parse_v1(rest, o) or parse_v2(rest, o)
        if r:
            out.append((o, r[0], r[1]))
            o = r[0]
            continue
        nxt = [x for x in (rest.find(b"\x00\x00\x00\xff", o + 1), rest.find(MAGIC_V2, o + 1)) if x > 0]
        if not nxt:
            break
        o = min(nxt)
    return out

# ---------------------------------------------------------------- 编码
def footer(n, seq=0, t0=316580000, t1=316590000):
    """24B 笔画身份：递增 id | 常量 19 | 起始时间 | 结束时间 | 8×00（时间像设备开机毫秒）。"""
    return _b32(0x2A000000 + n + seq) + _b32(19) + _b32(t0 + n) + _b32(t1 + n) + b"\x00" * 8

def encode_v1(points, width, color, seq=0):
    """points = [(x, y, pressure)] → v1 记录字节。"""
    n = len(points)
    body = b"".join(_f32(x) + _f32(y) + _f32(p) for x, y, p in points)
    return _b32(255) + _f32(width) + _b32(color & 0xFFFFFFFF) + _b32(n) + body + footer(n, seq)

def encode_v2(prefix, meta, points):
    """前缀（按类型取自 App 的真实记录）+ 13 字段 JSON + 点 → v2 记录字节。"""
    j = json.dumps(meta, **COMPACT).encode("utf-8")
    return prefix + _b32(len(j)) + j + _b32(len(points)) + b"".join(_f32(x) + _f32(y) for x, y in points)

def default_meta(color=0xFF000000, width=6.6, n=2, **kw):
    """v2 形状的 13 字段模板（取值语义见 README）。"""
    m = {"alpha": 255, "arrow": 0, "bottom": False, "cardUuid": "", "closePath": True,
         "color": int(color) - (1 << 32) if int(color) > 0x7FFFFFFF else int(color),
         "dotted": False, "fillColor": 0, "itemUuid": "", "mapUuid": "",
         "pointSize": n, "square": False, "width": float(width)}
    m.update(kw)
    return m

def default_pen_meta(color=0xFF000000, width=6.6, obj_id=None):
    """Pen 对象的字段模板（schema 与上面不同）。"""
    import random
    return {"alpha": 255, "angle": 0, "bottom": False,
            "color": int(color) - (1 << 32) if int(color) > 0x7FFFFFFF else int(color),
            "finalPathSmoothingMode": 0, "id": obj_id if obj_id is not None else random.randint(-2 ** 31, 2 ** 31 - 1),
            "previewPathSmoothingMode": 0, "smoothRenderPath": False, "type": 0, "width": float(width)}
