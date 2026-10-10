"""容器层：定位笔记、`.ncc` / `.p` 分段读写、`pathinfo` 索引。

盘上结构（WebDAV 空间根 → 笔记本目录）：

    享做笔记5/
      pathinfo/pathinfo.json    笔记本索引：infos[]（每篇一条）+ dirs[]（folder_root 的 children）
      <fileid>.ncc              笔记本体（fileid = note_<13位毫秒><6hex>）
      <fileid>__res__/
        <pageid>.p              一页的记录（pageid = note_page<13位毫秒><8hex>）
        page_usn.json           每页 usn（必须与页属性块里的 usn 相同）
        <pageid>/               空目录（占位）
        .note_<fileid>.pdf      导入的源 PDF（原文件）

`.ncc` = `[21B 文件头][u32 长度][JSON 元数据A][u32 1][u32 长度][JSON 字典][u32 长度][JSON 元数据B][u32][u32][u32 常量 92]`

`.p`   = `[u32 头长][头 JSON=视口][记录区][u32 长度][索引块][u32 长度][属性块][u32 头长][u32 记录区结束][u32 属性块起始][u32 0x1234]`
"""
import glob
import json
import os
import shutil
import struct
import time

from . import records

ROOT = "/data/netdisk/files/touchnotes"
NOTEBOOK = "享做笔记5"
NCC_CONST = 92
PAGE_MAGIC = 0x1234

_b32 = lambda n: struct.pack(">I", n)
_u32 = lambda b, o: struct.unpack(">I", b[o:o + 4])[0]

# ---------------------------------------------------------------- 路径与 id
def notebook_dir(root=None):
    return os.path.join(root or ROOT, NOTEBOOK)

def new_file_id(ms=None):
    return "note_%d%s" % (ms or int(time.time() * 1000), os.urandom(3).hex())

def new_page_id(ms=None):
    return "note_page%d%s" % (ms or int(time.time() * 1000), os.urandom(4).hex())

def new_note_id():
    return "note_" + os.urandom(16).hex()

# ---------------------------------------------------------------- .ncc
def parse_ncc(raw):
    """定位并解析 `.ncc` 里的 JSON 块（块之间可能夹一个 u32 分隔标记）。

    返回 dict(header, blocks, parts, tail, start)：
    parts = 各段的原始字节（含分隔标记），拼起来即原文件 —— 保证字节级可逆。
    """
    start = None
    for k in range(0, min(len(raw) - 4, 512)):
        L = _u32(raw, k)
        if not (2 < L < 10 ** 7) or k + 4 + L > len(raw):
            continue
        try:
            json.loads(raw[k + 4:k + 4 + L].decode("utf-8"))
            start = k
            break
        except Exception:
            continue
    if start is None:
        raise ValueError("不是有效的 .ncc（找不到 JSON 块）")
    parts, blocks, o = [], [], start
    while True:
        got = False
        for step in (0, 4):
            k = o + step
            if k + 4 > len(raw):
                break
            L = _u32(raw, k)
            if not (2 < L < 10 ** 7) or k + 4 + L > len(raw):
                continue
            try:
                j = json.loads(raw[k + 4:k + 4 + L].decode("utf-8"))
            except Exception:
                continue
            if step:
                parts.append(raw[o:k])
            parts.append(raw[k:k + 4 + L])
            blocks.append(j)
            o = k + 4 + L
            got = True
            break
        if not got:
            break
    return {"raw": raw, "start": start, "header": raw[:start], "parts": parts,
            "blocks": blocks, "tail": raw[o:]}

def read_ncc(path):
    return parse_ncc(open(path, "rb").read())

def build_ncc(tpl_raw, blocks, const=NCC_CONST):
    """用模板的文件头/尾 + 给定三块 JSON 生成 `.ncc` 字节。"""
    tpl = parse_ncc(tpl_raw)
    blobs = [json.dumps(b, **records.COMPACT).encode("utf-8") for b in blocks]
    off1 = len(tpl["header"]) + 4 + len(blobs[0]) + 4
    off2 = off1 + 4 + len(blobs[1])
    body = (tpl["header"] + _b32(len(blobs[0])) + blobs[0] + _b32(1)
            + _b32(len(blobs[1])) + blobs[1] + _b32(len(blobs[2])) + blobs[2]
            + _b32(off1) + _b32(off2) + _b32(const))
    return body

def patch_ncc(raw, index, block):
    """把第 index 个 JSON 块替换掉（其余字节原样）。"""
    tpl = parse_ncc(raw)
    n = 0
    out = tpl["header"]
    for p in tpl["parts"]:
        if p[:4] == b"":
            continue
        try:
            json.loads(p[4:].decode("utf-8"))
            is_json = True
        except Exception:
            is_json = False
        if is_json and n == index:
            blob = json.dumps(block, **records.COMPACT).encode("utf-8")
            out += _b32(len(blob)) + blob
            n += 1
        else:
            if is_json:
                n += 1
            out += p
    return out + tpl["tail"]

# ---------------------------------------------------------------- .p
def read_page(path):
    """解析一页：头(视口) / 记录（含索引给出的类型）/ 索引 / 属性 / 尾部三元组。

    记录边界以索引块 `ms[].s`(绝对偏移) / `ms[].c`(字节数) 为**准** —— 两者相加
    正好等于记录区全长；只按标记扫描会漏掉记录尾部的非记录字节（实测 App 页每页
    少 4~40 字节，导致重组后与原文件不等长）。
    """
    raw = open(path, "rb").read()
    hlen = _u32(raw, 0)
    head_raw = raw[4:4 + hlen]
    recs_end = _u32(raw, len(raw) - 12)
    props_at = _u32(raw, len(raw) - 8)
    ilen = _u32(raw, recs_end)
    index_raw = raw[recs_end + 4:recs_end + 4 + ilen]
    index = json.loads(index_raw.decode("utf-8"))
    plen = _u32(raw, props_at)
    props_raw = raw[props_at + 4:props_at + 4 + plen]
    props = json.loads(props_raw.decode("utf-8"))
    region = raw[4 + hlen:recs_end]
    out, ms = [], index.get("ms") or []
    for e in ms:
        s, c = e.get("s", -1), e.get("c", 0)
        if not (4 + hlen <= s and c > 0 and s + c <= recs_end):
            out = []
            break
        blob = raw[s:s + c]
        parsed = records.parse_v1(blob, 0) or records.parse_v2(blob, 0) or (None, {})
        tool = e.get("n", "?")
        out.append({"tool": tool, "kind": records.TOOL2KIND.get(tool, "?"), "offset": s,
                    "size": c, "raw": blob, "json": parsed[1].get("meta"),
                    "points": parsed[1].get("points", []), "width": parsed[1].get("width"),
                    "color": parsed[1].get("color"), "prefix": parsed[1].get("prefix")})
    if len(out) != len(ms):                      # 索引不可信 → 退回标记扫描
        out = []
        for i, (start, end, parsed) in enumerate(records.scan(region)):
            tool = ms[i].get("n", "?") if i < len(ms) else "?"
            out.append({"tool": tool, "kind": records.TOOL2KIND.get(tool, "?"),
                        "offset": 4 + hlen + start, "size": end - start, "raw": region[start:end],
                        "json": parsed.get("meta"), "points": parsed["points"],
                        "width": parsed.get("width"), "color": parsed.get("color"),
                        "prefix": parsed.get("prefix")})
    return {"path": path, "raw": raw, "head_raw": head_raw,
            "head": json.loads(head_raw.decode("utf-8")),
            "records": out, "region_raw": region, "index": index, "index_raw": index_raw,
            "props": props, "props_raw": props_raw, "trailer_raw": raw[-16:],
            "trailer": [_u32(raw, len(raw) - 16), recs_end, props_at, _u32(raw, len(raw) - 4)]}

def page_bytes(page, recs=None, props=None, index=None, head_raw=None, force=False):
    """重组 `.p`。

    什么都不改时直接返回原字节（必然一致）；`force=True` 走完整重组（用于自检）。
    尾部第一个字段是页面创建时的头长（陈旧值，App 改视口后不更新），因此原样保留。
    """
    changed = any(x is not None for x in (recs, props, index, head_raw))
    if not changed and not force:
        return page["raw"]
    head = head_raw if head_raw is not None else page["head_raw"]
    if recs is None:
        region = page["region_raw"]
        idx_raw = page["index_raw"] if index is None else index
    else:
        region = b"".join(b for _tool, b in recs)
        if index is not None:
            idx_raw = index
        else:
            ms, off = [], 4 + len(head)
            for tool, blob in recs:
                ms.append({"c": len(blob), "i": "", "n": tool, "s": off})
                off += len(blob)
            idx_raw = json.dumps({"materials": [], "ms": ms}, **records.COMPACT).encode("utf-8")
    props_raw = page["props_raw"] if props is None else json.dumps(props, **records.COMPACT).encode("utf-8")
    recs_end = 4 + len(head) + len(region)
    props_at = recs_end + 4 + len(idx_raw)
    trailer = page["trailer_raw"] if not changed else (_b32(_u32(page["raw"], len(page["raw"]) - 16))
                                                       + _b32(recs_end) + _b32(props_at) + _b32(PAGE_MAGIC))
    return (_b32(len(head)) + head + region
            + _b32(len(idx_raw)) + idx_raw
            + _b32(len(props_raw)) + props_raw + trailer)

# ---------------------------------------------------------------- 笔记索引
def read_pathinfo(root=None):
    return json.load(open(os.path.join(notebook_dir(root), "pathinfo", "pathinfo.json")))

def write_pathinfo(pi, root=None, backup_dir=None):
    p = os.path.join(notebook_dir(root), "pathinfo", "pathinfo.json")
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(p, os.path.join(backup_dir, "pathinfo-%d.json" % int(time.time() * 1000)))
    json.dump(pi, open(p, "w"), ensure_ascii=False, separators=(",", ":"))

def note_name(file_id, root=None):
    try:
        raw = open(os.path.join(notebook_dir(root), file_id + ".ncc"), "rb").read()
        return parse_ncc(raw)["blocks"][0].get("name", "")
    except Exception:
        return ""

def list_notes(root=None):
    d = notebook_dir(root)
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.ncc"))):
        fid = os.path.basename(p)[:-4]
        pages = sorted(glob.glob(os.path.join(d, fid + "__res__", "*.p")))
        size = os.path.getsize(p)
        for q in pages:
            size += os.path.getsize(q)
        out.append({"file_id": fid, "name": note_name(fid, root), "pages": len(pages),
                    "size": size, "mtime": os.path.getmtime(p)})
    return out

def find_note(sel, root=None):
    """按 file_id / .ncc 名 / 笔记名 / 唯一子串定位一篇笔记。"""
    notes = list_notes(root)
    for n in notes:
        if n["file_id"] == sel or n["file_id"] == "note_" + sel:
            return n
    hits = [n for n in notes if sel in n["file_id"] or sel in (n["name"] or "")]
    if not hits:
        raise LookupError("找不到笔记: %s" % sel)
    if len(hits) > 1:
        names = ", ".join("%s(%s)" % (h["name"], h["file_id"]) for h in hits[:6])
        raise LookupError("匹配到多篇，请写更精确的名字: " + names)
    return hits[0]

def page_paths(file_id, root=None):
    return sorted(glob.glob(os.path.join(notebook_dir(root), file_id + "__res__", "*.p")))

def page_order(file_id, root=None):
    """页序：优先按 `doc` 里的 index 排，其余按文件名补在后面。"""
    d = os.path.join(notebook_dir(root), file_id + "__res__")
    paths = page_paths(file_id, root)
    order = []
    p = os.path.join(d, "doc")
    if os.path.exists(p):
        try:
            order = [x["id"] for x in sorted(json.load(open(p))["pages"], key=lambda x: x["index"])]
        except Exception:
            order = []
    by_id = {}
    for q in paths:
        by_id[os.path.basename(q)[:-2]] = q
    out = [by_id[i] for i in order if i in by_id]
    out += [q for q in paths if q not in out]
    return out

def new_page(head_bytes, recs, props, trailer_first=None):
    """从零组装一页。recs = [(类型名, 记录字节)]。

    尾部第一个字段（页面创建时的头长，App 不更新）默认写成头长度 —— 与 App
    新建页的取值一致。
    """
    head = head_bytes
    region = b"".join(b for _tool, b in recs)
    ms, off = [], 4 + len(head)
    for tool, blob in recs:
        ms.append({"c": len(blob), "i": "", "n": tool, "s": off})
        off += len(blob)
    idx = json.dumps({"materials": [], "ms": ms}, **records.COMPACT).encode("utf-8")
    pb = json.dumps(props, **records.COMPACT).encode("utf-8")
    recs_end = 4 + len(head) + len(region)
    props_at = recs_end + 4 + len(idx)
    first = len(head) if trailer_first is None else trailer_first
    return (_b32(len(head)) + head + region + _b32(len(idx)) + idx + _b32(len(pb)) + pb
            + _b32(first) + _b32(recs_end) + _b32(props_at) + _b32(PAGE_MAGIC))

def source_pdf(file_id, root=None):
    d = os.path.join(notebook_dir(root), file_id + "__res__")
    hits = glob.glob(os.path.join(d, ".note_*.pdf"))
    return hits[0] if hits else None
