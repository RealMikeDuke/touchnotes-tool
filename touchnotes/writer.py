"""写回：规范化 JSON → App 能认的笔记文件（可选推送到网盘目录）。

**验证过的能力边界**（别指望更多）：

* v1 手写 —— 完全可写；带 `identity` 时字节级可重建。
* v2 形状 —— 可写，但**必须借 App 自己写过的同类型记录前缀**（前缀里还有未破解
  字段），字段用索引里给出的类型名决定画成什么。
* 笔记级一致性 —— `.ncc`（pageList/pageCount）+ 每页 `page_usn.json` + `pathinfo`
  三处必须同时对上，App 才认；写完会自动回读自检。
* 服务端**删不掉**笔记：App 本地有副本，下次同步会把文件重新传上来（实测）。

典型用法::

    from touchnotes import writer
    writer.build(json.load(open("note.json")), "/tmp/stage")      # 造到本地
    writer.push(manifest, backup_dir="/tmp/bak")                  # 推到网盘（需写权限）
"""
import json
import os
import shutil
import time

from . import container as C
from . import model as M
from . import records as R

IDENT_VIEWPORT = {"angle": 0.0, "offsetX": 0.0, "offsetY": 0.0, "rotateRect": None,
                  "scaleX": 1.0, "scaleY": 1.0}


def _signed(v):
    v = int(v)
    return v - (1 << 32) if v > 0x7FFFFFFF else v


def templates(root=None):
    """按类型收集 App 自己写过的记录前缀 + 字段模板（v1 手写不需要）。"""
    good, fallback = {}, {}
    for n in C.list_notes(root):
        for p in C.page_paths(n["file_id"], root):
            for r in C.read_page(p)["records"]:
                if r["kind"] in ("stroke", "pen") or not r["prefix"] or not r["json"]:
                    continue
                dst = fallback if r["json"].get("bottom") else good
                dst.setdefault(r["tool"], (r["prefix"], r["json"]))
    for k, v in fallback.items():
        good.setdefault(k, v)
    return good


def objects_to_records(objects, tpl, seq=0):
    """规范化对象 → [(类型名, 记录字节)]。"""
    out = []
    for i, o in enumerate(objects):
        kind = o.get("kind")
        if kind == "stroke":
            pts = [(float(x), float(y), float(p)) for x, y, p in o["points"]]
            blob = R.encode_v1(pts, float(o.get("width") or 3.0),
                               M.hex_color(o["color"], o.get("alpha", 255)), seq + i)
            if o.get("identity"):
                blob = blob[:-24] + bytes.fromhex(o["identity"])
            out.append(("FunPen", blob))
            continue
        tool = o.get("tool") or R.KIND2TOOL.get(kind)
        if tool not in tpl:
            raise KeyError("缺少 %s 类型的前缀模板：先在 App 里画一条该类型的记录" % tool)
        pre, base = tpl[tool]
        meta = dict(o.get("attrs") or base)
        meta.update({"color": _signed(M.hex_color(o["color"], o.get("alpha", 255))),
                     "alpha": int(o.get("alpha", 255)),
                     "width": float(o.get("width") or base.get("width") or 6.6),
                     "pointSize": len(o["points"]),
                     "closePath": bool(o.get("closed", True)),
                     "arrow": int(o.get("arrow") or 0),
                     "dotted": bool(o.get("dotted")),
                     "square": bool(o.get("square"))})
        if o.get("fill"):
            meta["fillColor"] = _signed(M.hex_color(o["fill"]))
        out.append((tool, R.encode_v2(pre, meta, [(float(x), float(y)) for x, y in o["points"]])))
    return out


def _template_ref(root=None):
    """结构模板：借用现有笔记的 .ncc 头/尾与一份页属性。"""
    notes = C.list_notes(root)
    if not notes:
        raise LookupError("笔记本里没有任何笔记，无法取得结构模板")
    ref = notes[-1]["file_id"]
    ncc = open(os.path.join(C.notebook_dir(root), ref + ".ncc"), "rb").read()
    pages = C.page_paths(ref, root)
    props = C.read_page(pages[-1])["props"] if pages else {}
    return ref, ncc, props


def build(spec, out_dir, name=None, root=None, tpl=None, ms=None):
    """按规范化 JSON 造一篇笔记到 `out_dir`（不动网盘）。返回 manifest。"""
    tpl = tpl if tpl is not None else templates(root)
    ref, ref_ncc, props_tpl = _template_ref(root)
    ms = ms or int(time.time() * 1000)
    file_id = C.new_file_id(ms)
    note_id = C.new_note_id()
    name = name or (spec.get("note") or {}).get("name") or "tn-import"
    head = json.dumps(IDENT_VIEWPORT, **R.COMPACT).encode("utf-8")
    pages = spec.get("pages") or []
    if not pages:
        raise ValueError("spec 里没有任何页")
    staged = []
    for i, pg in enumerate(pages):
        objs = pg.get("objects") or []
        recs = objects_to_records(objs, tpl, seq=i * 1000)
        pid = C.new_page_id(ms + i)
        props = dict(props_tpl)
        import uuid
        props.update({"id": pid, "index": i, "date": ms, "usn": 1, "materialSize": len(recs),
                      "limited": True, "shapeMatrix": dict(IDENT_VIEWPORT),
                      "layerInfos": [{"end": 0, "ends": [len(recs)], "layerUUID": str(uuid.uuid4()),
                                      "name": "Layer1", "show": True, "start": 0, "starts": [0]}]})
        staged.append((pid, C.new_page(head, recs, props), len(objs)))
    # 组装 .ncc
    blocks = C.parse_ncc(ref_ncc)["blocks"]
    md0 = dict(blocks[0])
    md0.update({"id": note_id, "name": name, "date": ms, "pageCount": len(staged), "usn": 1,
                "synced": False, "lastSyncUSN": 0, "netDirty": False, "pdfUSN": 0,
                "foundPdf": False, "originalPath": "", "tags": [], "favor": False,
                "pageIndex": 0, "pageIndex2": 0, "pdf": "", "res": file_id + "__res__",
                "notePath": "/storage/emulated/0/touchnotes/note/%s.ncc" % file_id})
    dict_blk = {"md5": "nc", "pageList": [p[0] for p in staged], "pages": []}
    md1 = dict(md0)
    md1.update({"synced": True, "lastSyncUSN": 1})
    ncc = C.build_ncc(ref_ncc, [md0, dict_blk, md1])
    # 落盘
    res = os.path.join(out_dir, file_id + "__res__")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(res, exist_ok=True)
    open(os.path.join(out_dir, file_id + ".ncc"), "wb").write(ncc)
    man_pages = []
    for pid, blob, n_obj in staged:
        p = os.path.join(res, pid + ".p")
        open(p, "wb").write(blob)
        os.makedirs(os.path.join(res, pid), exist_ok=True)
        man_pages.append({"index": None, "id": pid, "path": p, "objects": n_obj,
                          "bytes": len(blob), "sha": __import__("hashlib").sha256(blob).hexdigest()[:16]})
    with open(os.path.join(res, "page_usn.json"), "w") as f:
        json.dump({"infos": [{"customPage": "", "di": -1, "id": pid, "pdf": "", "res": pid + ".p",
                              "usn": 1} for pid, _b, _n in staged]}, f, **R.COMPACT)
    manifest = {"file_id": file_id, "note_id": note_id, "name": name, "dir": out_dir,
                "ncc": os.path.join(out_dir, file_id + ".ncc"), "res": res,
                "pages": man_pages, "template_ref": ref,
                "types": sorted({t for pg in pages for t in
                                 [(o.get("tool") or "FunPen") for o in (pg.get("objects") or [])]})}
    manifest["self_check"] = verify_build(manifest, spec)
    return manifest


def verify_build(manifest, spec):
    """回读生成的文件，核对条数/类型/点数（坐标按 JSON 精度容差比对）。"""
    errs = []
    for i, (mp, pg) in enumerate(zip(manifest["pages"], spec.get("pages") or [])):
        page = C.read_page(mp["path"])
        if len(page["records"]) != len(pg.get("objects") or []):
            errs.append("第%d页 条数 %d≠%d" % (i + 1, len(page["records"]), len(pg["objects"] or [])))
            continue
        for j, (r, o) in enumerate(zip(page["records"], pg["objects"] or [])):
            want = len(o["points"])
            if len(r["points"]) != want:
                errs.append("第%d页 第%d条 点数 %d≠%d" % (i + 1, j + 1, len(r["points"]), want))
            if o.get("kind") == "stroke":
                if abs(float(r["width"]) - float(o.get("width") or 3.0)) > 1e-4:
                    errs.append("第%d页 第%d条 笔宽不符" % (i + 1, j + 1))
            elif r["tool"] != (o.get("tool") or R.KIND2TOOL.get(o.get("kind"))):
                errs.append("第%d页 第%d条 类型 %s≠%s" % (i + 1, j + 1, r["tool"], o.get("tool")))
            else:
                dev = max(abs(r["points"][k][m] - float(o["points"][k][m]))
                          for k in range(len(r["points"])) for m in range(len(r["points"][k])))
                if dev > 0.002:
                    errs.append("第%d页 第%d条 坐标偏差 %.4g" % (i + 1, j + 1, dev))
        if C.page_bytes(page, force=True) != page["raw"]:
            errs.append("第%d页 字节级回合不符" % (i + 1))
    return {"ok": not errs, "errors": errs,
            "pages": len(manifest["pages"]), "objects": sum(p["objects"] for p in manifest["pages"])}


def push(manifest, root=None, backup_dir=None, pathinfo=True):
    """把 stage 目录推进网盘笔记本目录（需要写权限，通常用 sudo 跑）。"""
    dst = C.notebook_dir(root)
    if not os.access(dst, os.W_OK):
        raise PermissionError("没有写权限：%s（用 sudo 跑，或改 root）" % dst)
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(os.path.join(dst, "pathinfo", "pathinfo.json"),
                     os.path.join(backup_dir, "pathinfo-%d.json" % int(time.time() * 1000)))
    shutil.copytree(manifest["dir"], dst, dirs_exist_ok=True)
    for base in (manifest["ncc"], manifest["res"]):
        os.chmod(base, 0o644 if os.path.isfile(base) else 0o755)
    for r_, dirs, files in os.walk(manifest["res"]):
        os.chmod(r_, 0o755)
        for fn in files:
            os.chmod(os.path.join(r_, fn), 0o644)
    if not pathinfo:
        return manifest
    hp = os.path.join(dst, "pathinfo", "pathinfo.json")
    pi = json.load(open(hp))
    rel = "note/%s.ncc" % manifest["file_id"]
    pi["infos"].append({"cc": 0, "cd": False, "children": [], "chp": 1, "clt": 0, "cv": "",
                        "del": False, "favor": False, "fn": "", "id": manifest["note_id"],
                        "lUsn": 1, "lmf": int(time.time() * 1000), "n": manifest["file_id"] + ".ncc",
                        "nn": manifest["name"], "oPid": "", "path": rel, "pid": "folder_root",
                        "rcl": False, "usn": 1, "vPath": rel})
    for d in pi.get("dirs", []):
        if d.get("id") == "folder_root":
            d["children"] = [manifest["note_id"]] + [c for c in d.get("children", [])
                                                     if c != manifest["note_id"]]
    json.dump(pi, open(hp, "w"), ensure_ascii=False, separators=(",", ":"))
    return manifest


def remove(file_id, root=None, backup_dir=None):
    """从笔记本目录移除一篇笔记（清 pathinfo 条目；服务端删除会被 App 同步回来）。"""
    dst = C.notebook_dir(root)
    hp = os.path.join(dst, "pathinfo", "pathinfo.json")
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(hp, os.path.join(backup_dir, "pathinfo-%d.json" % int(time.time() * 1000)))
    pi = json.load(open(hp))
    gone = [e["id"] for e in pi["infos"] if e.get("n") == file_id + ".ncc"]
    pi["infos"] = [e for e in pi["infos"] if e.get("n") != file_id + ".ncc"]
    for d in pi.get("dirs", []):
        d["children"] = [c for c in d.get("children", []) if c not in gone]
    json.dump(pi, open(hp, "w"), ensure_ascii=False, separators=(",", ":"))
    for p in (os.path.join(dst, file_id + ".ncc"), os.path.join(dst, file_id + "__res__")):
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
    return {"file_id": file_id, "pathinfo_entries_removed": len(gone)}
