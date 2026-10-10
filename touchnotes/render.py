"""渲染：栅格（PNG / 栅格 PDF）与矢量（SVG / HTML / 矢量 PDF）。

移植自已验证的两套导出实现，并修正一处旧实现的缺陷：v2 形状以前一律当成
「两点直线」画，于是三角形缺一条边、圆和波浪被画成直线。现在按类型展开真实几何：

    circle     圆心 + 圆上一点 → 真正的圆（96 段折线）
    wave       两点 → 正弦（振幅 = 距离 × 0.12，5 个周期；App 内部参数未知，这是近似）
    bezier     3 点 → 二次贝塞尔采样
    polygon    按 closePath 真正闭合
    arrow      1 = 画在最后一点，2 = 两端都画
    dotted     虚线（栅格按段切开，矢量用 dasharray）
"""
import glob
import io
import json
import math
import os
import re
import subprocess

from . import geometry as G

HAS_PDFTOOLS = bool(__import__("shutil").which("pdftoppm"))

# ---------------------------------------------------------------- 颜色
def argb(c):
    """App 的 ARGB 整数 → (r, g, b, a)。"""
    c = int(c)
    if c < 0:
        c += 1 << 32
    return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF, ((c >> 24) & 0xFF) or 255)

def hexcol(rgba):
    return "#%02x%02x%02x" % rgba[:3]

# ---------------------------------------------------------------- 形状展开
def expand_shape(kind, pts, closed, nseg=96):
    """把一条 v2 形状展开成折线点列。"""
    if kind == "circle" and len(pts) >= 2:
        (cx, cy), (px, py) = pts[0], pts[1]
        r = math.dist((cx, cy), (px, py))
        return [(cx + r * math.cos(2 * math.pi * i / nseg), cy + r * math.sin(2 * math.pi * i / nseg))
                for i in range(nseg + 1)], True
    if kind == "wave" and len(pts) >= 2:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        d = math.dist((x0, y0), (x1, y1)) or 1.0
        amp, cyc = d * 0.12, 2.5
        out = []
        for i in range(121):
            t = i / 120.0
            ux, uy = (x1 - x0), (y1 - y0)
            nx, ny = -uy / d, ux / d
            off = amp * math.sin(2 * math.pi * cyc * t)
            out.append((x0 + ux * t + nx * off, y0 + uy * t + ny * off))
        return out, False
    if kind == "bezier" and len(pts) == 3:
        (x0, y0), (cx, cy), (x1, y1) = pts
        return [((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1,
                 (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1)
                for t in [i / 60.0 for i in range(61)]], False
    return list(pts), bool(closed)

def page_shapes(page):
    """把一页的记录统一成可绘制对象列表。

    每项: {color:(r,g,b,a), width:float, pts:[(x,y,pressure)], closed:bool,
           dotted:bool, arrow:int, square:bool, fill:(r,g,b,a)|None, kind:str}
    """
    out = []
    for r in page["records"]:
        if r["kind"] == "stroke":
            out.append({"color": argb(r["color"]), "width": float(r["width"] or 3.0),
                        "pts": list(r["points"]), "closed": False, "dotted": False,
                        "arrow": 0, "square": False, "fill": None, "kind": "stroke"})
            continue
        m = r["json"] or {}
        pts, closed = expand_shape(r["kind"], list(r["points"]), m.get("closePath", True))
        pts = [(x, y, 1.0) for x, y in pts]
        out.append({"color": argb(m.get("color", -16777216)), "width": float(m.get("width") or 3.0),
                    "pts": pts, "closed": closed, "dotted": bool(m.get("dotted")),
                    "arrow": int(m.get("arrow") or 0), "square": bool(m.get("square")),
                    "fill": argb(m["fillColor"]) if m.get("fillColor") else None,
                    "alpha": int(m.get("alpha", 255)), "kind": r["kind"]})
    return out

# ---------------------------------------------------------------- 画布
def canvas(page, has_bg=False, margin=40.0, mode="auto"):
    """画布尺寸与平移量（与旧导出实现同一套规则）。

    有底图的页：用元数据尺寸、原点不动，保证笔迹与底图 1:1 对齐；
    纯笔迹页：无限画布、坐标可为负，用墨迹包围盒 + 边距（含最粗笔半宽）并平移。
    """
    W = float(page["props"].get("width") or 0)
    H = float(page["props"].get("height") or 0)
    if mode == "declared" and W > 0 and H > 0:
        return W, H, 0.0, 0.0
    has_bg = has_bg and mode != "ink"
    objs = page_shapes(page)
    xs = [p[0] for o in objs for p in o["pts"]]
    ys = [p[1] for o in objs for p in o["pts"]]
    if has_bg:
        if W <= 0:
            W, H = 1626.0, 2105.0
        if xs:
            hw2 = max((o["width"] / 2.0 for o in objs), default=0.0)
            W, H = max(W, max(xs) + margin + hw2), max(H, max(ys) + margin + hw2)
        return W, H, 0.0, 0.0
    if not xs:
        return (W or 1626.0), (H or 2105.0), 0.0, 0.0
    hwmax = max((o["width"] / 2.0 for o in objs), default=0.0)
    m = margin + hwmax
    x0, y0 = min(xs) - m, min(ys) - m
    return (max(xs) - x0 + m), (max(ys) - y0 + m), x0, y0

def fit_ss(W, H, ss_cap=3):
    """超采样倍数按画布面积自适应：超采样只为笔迹抗锯齿，页面越大收益越小、代价越大。"""
    mp = W * H / 1e6
    return ss_cap if mp <= 4 else (2 if mp <= 12 else 1)

# ---------------------------------------------------------------- 底图
def raster_pdf_page(pdf, page_no, W, H, tmp_dir, ss=1, tag="bg"):
    """把源 PDF 的第 page_no 页（1 基）渲染成 W×H 的 RGB 图。"""
    from PIL import Image
    if not HAS_PDFTOOLS:
        return None, "未安装 poppler-utils（pdftoppm）"
    base = os.path.join(tmp_dir, "%s_p%d" % (tag, page_no))
    for f in glob.glob(base + "*"):
        os.remove(f)
    dpi = int(round(W * ss / (612.0 / 72.0)))
    r = subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page_no), "-l", str(page_no),
                        pdf, base], capture_output=True, text=True)
    got = sorted(glob.glob(base + "*.png"))
    if not got:
        return None, (r.stderr or "pdftoppm 无输出").strip()
    im = Image.open(got[0]).convert("RGB")
    if im.size != (int(W * ss), int(H * ss)):
        im = im.resize((int(W * ss), int(H * ss)), Image.LANCZOS)
    return im, None

# ---------------------------------------------------------------- 栅格绘制
def _draw_path(d, obj, dx, dy, ss, gain=1.0, color=None):
    """画一条路径（变宽带 + 每点补圆，避免 even-odd 空洞与接缝漏白）。"""
    col = color or obj["color"]
    pts = [(x - dx, y - dy, p) for x, y, p in obj["pts"]]
    if len(pts) < 2:
        if pts:
            x, y = pts[0][0] * ss, pts[0][1] * ss
            r = max(1.0, obj["width"] / 2 * ss)
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        return
    hw = G.halfwidths(obj["width"], [p[2] for p in pts], gain=gain)
    for _ in range(2):                      # 平滑半宽序列，去掉抖动造成的锯齿状粗细
        hw = [hw[0]] + [(hw[i - 1] + hw[i] + hw[i + 1]) / 3.0 for i in range(1, len(hw) - 1)] + [hw[-1]]
    P = [(x * ss, y * ss) for x, y, _ in pts]
    if obj["closed"]:
        P = P + [P[0]]
        hw = hw + [hw[0]]
    if obj.get("fill"):
        d.polygon(P, fill=obj["fill"])
    step = max(1, int(obj["width"] * 2.2))  # 虚线：按段切开
    if obj["dotted"]:
        for i in range(0, len(P) - 1, step * 2):
            seg = P[i:i + step + 2]
            if len(seg) > 1:
                d.line(seg, fill=col, width=max(2, int(round(hw[i] * 2))))
        return
    for i in range(len(P) - 1):
        w = max(2, int(round(hw[min(i, len(hw) - 1)] + hw[min(i + 1, len(hw) - 1)])))
        d.line([P[i], P[i + 1]], fill=col, width=w)
    for i, (x, y) in enumerate(P):
        r = max(1.0, hw[min(i, len(hw) - 1)])
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)

def _draw_arrow(d, obj, dx, dy, ss, color):
    """箭头：画在最后一点（arrow=1）或两端（arrow=2）。"""
    pts = [(x - dx, y - dy) for x, y, _ in obj["pts"]]
    if len(pts) < 2:
        return
    size = max(6.0, obj["width"] * 3.0) * ss
    heads = [1] if obj["arrow"] == 1 else ([0, 1] if obj["arrow"] == 2 else [])
    for end in heads:
        (x1, y1), (x2, y2) = (pts[-2], pts[-1]) if end == 1 else (pts[1], pts[0])
        a = math.atan2(y2 - y1, x2 - x1)
        tip = (x2 * ss, y2 * ss)
        d.polygon([tip,
                   (tip[0] - size * math.cos(a - 0.42), tip[1] - size * math.sin(a - 0.42)),
                   (tip[0] - size * math.cos(a + 0.42), tip[1] - size * math.sin(a + 0.42))], fill=color)

def render_page(page, page_no=1, bg_pdf=None, ss_cap=3, gain=1.0, margin=40.0, tmp_dir=None,
                mode="auto"):
    """渲染一页 → PIL RGB 图（1 单位 = 1 像素）。"""
    from PIL import Image, ImageDraw
    W, H, dx, dy = canvas(page, has_bg=bool(bg_pdf), margin=margin, mode=mode)
    ss = fit_ss(W, H, ss_cap)
    if bg_pdf:
        bw = float(page["props"].get("width") or 0) or 1626.0
        bh = float(page["props"].get("height") or 0) or 2105.0
        img = Image.new("RGB", (int(W * ss), int(H * ss)), "white")
        bg, err = raster_pdf_page(bg_pdf, page_no, bw, bh, tmp_dir or "/tmp", ss=ss, tag="tnbg")
        if bg is not None:
            img.paste(bg.resize((int(bw * ss), int(bh * ss)), Image.LANCZOS), (0, 0))
    else:
        img = Image.new("RGB", (int(W * ss), int(H * ss)), "white")
    d = ImageDraw.Draw(img, "RGBA")
    for obj in page_shapes(page):
        _draw_path(d, obj, dx, dy, ss, gain=gain)
        if obj["arrow"]:
            _draw_arrow(d, obj, dx, dy, ss, obj["color"])
    return img.resize((int(W), int(H)), Image.LANCZOS)

# ---------------------------------------------------------------- 矢量（描边，不做轮廓填充）
def _stroke_runs(pts, hw, q=0.35):
    """把一笔按量化后的半宽切成若干连续段：(半宽, [点...])。"""
    runs, cur_w, cur = [], None, []
    for i in range(len(pts)):
        x, y = pts[i][0], pts[i][1]
        w = round(hw[i] / q) * q
        if cur_w is None:
            cur_w, cur = w, [(x, y)]
        elif abs(w - cur_w) < 1e-9:
            cur.append((x, y))
        else:
            cur.append((x, y))
            runs.append((cur_w, cur))
            cur_w, cur = w, [(x, y)]
    if cur:
        runs.append((cur_w, cur))
    return runs

def page_svg_fragments(page, dx, dy, gain=1.0, y_off=0.0):
    out = []
    for obj in page_shapes(page):
        col = hexcol(obj["color"])
        pts = [(x - dx, y - dy) for x, y, _ in obj["pts"]]
        hw = G.halfwidths(obj["width"], [p[2] for p in obj["pts"]], gain=gain)
        if obj["closed"] and pts:
            pts = pts + [pts[0]]
            hw = hw + [hw[0]]
        if obj.get("fill") and obj["closed"]:
            d0 = "M" + " L".join("%.2f,%.2f" % (x, y + y_off) for x, y in pts)
            out.append('<path d="%s Z" fill="%s" stroke="none"/>' % (d0, hexcol(obj["fill"])))
        dash = ' stroke-dasharray="%.2f %.2f"' % (obj["width"] * 2.2, obj["width"] * 1.8) if obj["dotted"] else ""
        cap = "" if obj["square"] else ' stroke-linecap="round" stroke-linejoin="round"'
        for w, run in _stroke_runs(pts, hw):
            d = "M" + " L".join("%.2f,%.2f" % (x, y + y_off) for x, y in run)
            out.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f"%s%s/>' % (d, col, 2 * w, cap, dash))
        if obj["arrow"] and len(pts) >= 2:
            size = max(6.0, obj["width"] * 3.0)
            for end in ([1] if obj["arrow"] == 1 else [0, 1]):
                (x1, y1), (x2, y2) = (pts[-2], pts[-1]) if end else (pts[1], pts[0])
                a = math.atan2(y2 - y1, x2 - x1)
                p3 = (x2 - size * math.cos(a - 0.42), y2 - size * math.sin(a - 0.42))
                p4 = (x2 - size * math.cos(a + 0.42), y2 - size * math.sin(a + 0.42))
                out.append('<path d="M%.2f,%.2f L%.2f,%.2f L%.2f,%.2f Z" fill="%s"/>'
                           % (x2, y2 + y_off, p3[0], p3[1] + y_off, p4[0], p4[1] + y_off, col))
    return out

def svg(page, has_bg=False, title="", gain=1.0, mode="auto"):
    W, H, dx, dy = canvas(page, has_bg=has_bg, mode=mode)
    body = ['<rect x="0" y="0" width="%.2f" height="%.2f" fill="#ffffff"/>' % (W, H)]
    body += page_svg_fragments(page, dx, dy, gain=gain)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.0f %.0f" width="%.0f" height="%.0f">\n'
            '<title>%s</title>\n%s\n</svg>\n' % (W, H, W, H, title, "\n".join(body)))

def html(pages_meta, gain=1.0, mode="auto"):
    """pages_meta = [(name, page, has_bg)] → 一个把每页排成卡片的 HTML。"""
    body = []
    for name, page, has_bg in pages_meta:
        W, H, dx, dy = canvas(page, has_bg=has_bg, mode=mode)
        body.append('<div class="card"><div class="cap">%s</div>' % name)
        body.append('<svg viewBox="0 0 %.0f %.0f" width="%.0f" xmlns="http://www.w3.org/2000/svg">' % (W, H, W * 0.28))
        body.append('<rect width="%.0f" height="%.0f" fill="#fff"/>' % (W, H))
        body += page_svg_fragments(page, dx, dy, gain=gain)
        body.append("</svg></div>")
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>TouchNotes vector export</title>\n<style>\n"
            " body{background:#111;color:#ddd;font:14px/1.5 system-ui,sans-serif;margin:24px}\n"
            " h1{font-size:18px} .card{display:inline-block;margin:0 16px 16px 0;vertical-align:top}\n"
            " .cap{font-size:12px;color:#8ab;margin:0 0 6px} svg{background:#fff;border-radius:6px;"
            "box-shadow:0 2px 10px #0008}\n</style></head><body>\n<h1>TouchNotes vector export</h1>\n"
            "<p>%d pages · ink is vector (stroked paths), no rasterisation.</p>\n%s\n</body></html>\n"
            % (len(pages_meta), "\n".join(body)))

# ---------------------------------------------------------------- 矢量 PDF（手写最小写入器）
def _pdf_obj(n, body):
    return ("%d 0 obj\n%s\nendobj\n" % (n, body)).encode("latin-1")

def pdf(pages_meta, bg_pdf=None, tmp_dir=None, gain=1.0, mode="auto"):
    """pages_meta = [(page_no, page, has_bg)] → 矢量 PDF 字节。
    有源 PDF 的页：底图用 JPEG（DCTDecode）贴，笔迹仍是矢量。"""
    from PIL import Image
    objs, kids, nxt = {}, [], 3
    for page_no, page, has_bg in pages_meta:
        W, H, dx, dy = canvas(page, has_bg=has_bg, mode=mode)
        S = 612.0 / max(W, 1.0)                 # 统一按 Letter 宽度归一，保持比例
        PW, PH = W * S, H * S
        content = ["q %.6f 0 0 %.6f 0 %.4f cm" % (S, -S, PH)]
        imgref = None
        if bg_pdf and has_bg:
            im, err = raster_pdf_page(bg_pdf, page_no, W, H, tmp_dir or "/tmp", ss=1, tag="tnvec")
            if im is not None:
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                data = buf.getvalue()
                objs[nxt] = (("%d 0 obj\n<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                              "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                              "/Length %d >>\nstream\n") % (nxt, im.width, im.height, len(data))
                             ).encode("latin-1") + data + b"\nendstream\nendobj\n"
                imgref = nxt
                nxt += 1
                content.append("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (W, H))
        content.append("1 J 1 j")               # round cap / round join
        for obj in page_shapes(page):
            r, g, b = [v / 255.0 for v in obj["color"][:3]]
            pts = [(x - dx, y - dy) for x, y, _ in obj["pts"]]
            hw = G.halfwidths(obj["width"], [p[2] for p in obj["pts"]], gain=gain)
            if obj["closed"] and pts:
                pts = pts + [pts[0]]
                hw = hw + [hw[0]]
            if obj["dotted"]:
                # 虚线：栅格化用短段模拟（PDF 的 dash 需要维护图形状态，这里简单切段）
                step = max(1, int(obj["width"] * 2.2))
                runs = [(hw[i], pts[i:i + step + 2]) for i in range(0, len(pts) - 1, step * 2)]
            else:
                runs = _stroke_runs(pts, hw)
            for w, run in runs:
                if len(run) < 2:
                    continue
                d_ = "%.2f %.2f m " % run[0] + " ".join("%.2f %.2f l" % (x, y) for x, y in run[1:])
                content.append("%.4f %.4f %.4f RG %.3f w %s S" % (r, g, b, 2 * w, d_))
        content.append("Q")
        cs = "\n".join(content).encode("latin-1")
        objs[nxt] = (("%d 0 obj\n<< /Length %d >>\nstream\n" % (nxt, len(cs))).encode("latin-1")
                     + cs + b"\nendstream\nendobj\n")
        cnum = nxt
        nxt += 1
        res = "<< /XObject << /Im0 %d 0 R >> >>" % imgref if imgref else "<< >>"
        objs[nxt] = _pdf_obj(nxt, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
                                 "/Resources %s /Contents %d 0 R >>" % (PW, PH, res, cnum))
        kids.append(nxt)
        nxt += 1
    objs[1] = _pdf_obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
    objs[2] = _pdf_obj(2, "<< /Type /Pages /Count %d /Kids [%s] >>"
                       % (len(kids), " ".join("%d 0 R" % k for k in kids)))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += objs[n]
    xref = len(out)
    mx = max(objs) + 1
    out += ("xref\n0 %d\n0000000000 65535 f \n" % mx).encode("latin-1")
    for n in range(1, mx):
        out += ("%010d 00000 n \n" % offsets.get(n, 0)).encode("latin-1")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (mx, xref)).encode("latin-1")
    return bytes(out)

def pdf_page_count(pdf_path):
    if not HAS_PDFTOOLS:
        return 0
    try:
        info = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True).stdout
        return int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1))
    except Exception:
        return 0
