"""几何与压感：重采样、压感→笔宽、变宽带顶点。

App 实测采样密度：相邻点间隔中位 0.66 单位（≈0.088mm，每英寸约 288 点）；
相邻点连线偏离真实曲线的垂距中位仅 1.2 微米 —— 所以生成笔迹时不必上贝塞尔平滑，
按接近原始密度重采样即可。
"""
import math

# ---------------------------------------------------------------- 重采样
def densify(pts, step=1.1):
    """在折线上按 step 间距插值补点（模拟 App 的高密度采样）。"""
    out = []
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        d = math.dist((x0, y0), (x1, y1))
        k = max(1, int(d / step))
        for j in range(k):
            t = j / k
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    out.append(pts[-1])
    return out

# ---------------------------------------------------------------- 压感曲线
def press_ramp(n, lo=0.45, hi=1.0):
    """线性渐变（起笔轻、收笔重）。"""
    return [lo + (hi - lo) * (i / max(1, n - 1)) for i in range(n)]

def press_wave(n, base=0.55, amp=0.35, cyc=2.0):
    """正弦起伏，最接近手写的自然压感。"""
    return [max(0.18, min(1.0, base + amp * math.sin(2 * math.pi * cyc * i / max(1, n)))) for i in range(n)]

def press_const(n, v=0.8):
    return [v] * n

# ---------------------------------------------------------------- 压感→笔宽
def halfwidths(width, pressures, gain=1.0, floor=0.18, ceil=1.15):
    """逐点半宽。

    以该笔压感的第 90 百分位为「满宽」（最粗处 = 笔的设定宽度），细处自然收笔；
    下限 floor 防止极端压感把笔画细到看不见。实测同一笔 最宽/最窄 中位 3.4x。
    """
    ps = [p for p in pressures if p > 0] or [1.0]
    ref = sorted(ps)[min(len(ps) - 1, int(0.9 * len(ps)))]
    ref = max(ref, 1e-6)
    out = []
    for p in pressures:
        k = min(ceil, max(floor, (p if p > 0 else ref) / ref))
        out.append(max(0.5, width / 2.0 * k * gain))
    return out

def band_segments(pts, hw):
    """把变宽笔画拆成「逐段四边形 + 每点圆点」。

    不能用整条自交多边形一次填充：折返笔画会让左右轮廓交叉，even-odd 规则会在
    交叉区留下成片透白空洞。逐段四边形永远是凸的，不可能自交。
    返回 (quads, dots)：quads = [[4 个角点], ...]，dots = [(x, y, r), ...]
    """
    quads, dots = [], []
    for i, (x, y) in enumerate(pts):
        r = hw[i]
        dots.append((x, y, r))
        if i == 0:
            continue
        x0, y0 = pts[i - 1]
        r0 = hw[i - 1]
        dx, dy = x - x0, y - y0
        L = math.hypot(dx, dy) or 1e-6
        nx, ny = -dy / L, dx / L
        quads.append([(x0 + nx * r0, y0 + ny * r0), (x0 - nx * r0, y0 - ny * r0),
                      (x - nx * r, y - ny * r), (x + nx * r, y + ny * r)])
    return quads, dots

# ---------------------------------------------------------------- 常用形状
def star_polygon(cx, cy, r, inner=0.42, points=5):
    """五角星顶点（2×points 个，外/内半径交替）。"""
    out = []
    for i in range(points * 2):
        rr = r if i % 2 == 0 else r * inner
        a = -math.pi / 2 + i * math.pi / points
        out.append((round(cx + rr * math.cos(a), 1), round(cy + rr * math.sin(a), 1)))
    return out

def circle_handle(cx, cy, r):
    """圆的记录用「圆心 + 圆上一点」两个点。"""
    return [(cx, cy), (cx + r, cy)]
