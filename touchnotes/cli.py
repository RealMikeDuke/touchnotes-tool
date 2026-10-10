"""命令行入口：`tn <子命令>`（见 README）。"""
import argparse
import json
import os
import sys
import time

from . import container as C
from . import model as M
from . import render as RD
from . import records as R
from . import writer as W

DEFAULT_ROOT = C.ROOT
DEFAULT_TMP = os.path.join(os.path.expanduser("~"), ".hermes", "cache", "scratch", "tn-tmp")


def _fmt_size(n):
    return "%d B" % n if n < 10240 else "%.1f KiB" % (n / 1024.0)


def _page_of(path, root):
    if path:
        return C.read_page(path)
    return {"records": [], "props": {"width": 1626.0, "height": 2105.0}, "head": {},
            "raw": b"", "region_raw": b"", "index": {}, "index_raw": b"", "props_raw": b"",
            "trailer_raw": b"", "trailer": [0, 0, 0, 0], "path": None}


def _note_frames(note, root, only=None):
    """要导出的页：[(页序号(1基), 页文件 | None)]，PDF 笔记按源 PDF 页数出页。"""
    fid = note["file_id"]
    src = C.source_pdf(fid, root)
    paths = C.page_order(fid, root)
    npdf = RD.pdf_page_count(src) if src else 0
    if src and npdf:
        items = [(i + 1, paths[i] if i < len(paths) else None) for i in range(npdf)]
    else:
        items = [(i + 1, p) for i, p in enumerate(paths)]
    if only:
        items = [it for it in items if it[0] == only]
    return fid, src, items


# ---------------------------------------------------------------- 子命令
def cmd_list(a):
    notes = C.list_notes(a.root)
    if a.json:
        print(json.dumps(notes, ensure_ascii=False, indent=1))
        return
    print("%-30s %-22s %4s %10s  %s" % ("file_id", "名称", "页", "大小", "修改时间"))
    for n in notes:
        print("%-30s %-22s %4d %10s  %s" % (n["file_id"], (n["name"] or "")[:22], n["pages"],
                                            _fmt_size(n["size"]),
                                            time.strftime("%m-%d %H:%M", time.localtime(n["mtime"]))))
    print("\n共 %d 篇；根目录 %s" % (len(notes), C.notebook_dir(a.root)))


def cmd_info(a):
    note = C.find_note(a.note, a.root)
    fid = note["file_id"]
    src = C.source_pdf(fid, a.root)
    rows, tot_r, tot_p = [], 0, 0
    for i, p in enumerate(C.page_order(fid, a.root), 1):
        pg = C.read_page(p)
        kinds = {}
        for r in pg["records"]:
            kinds[r["tool"]] = kinds.get(r["tool"], 0) + 1
        np_ = sum(len(r["points"]) for r in pg["records"])
        idx = pg["props"].get("index")
        canvas = "%g×%g" % (pg["props"].get("width", 0), pg["props"].get("height", 0))
        rows.append((i, os.path.basename(p), idx, len(pg["records"]), np_, canvas, kinds,
                     pg["props"].get("usn")))
        tot_r += len(pg["records"])
        tot_p += np_
    if a.json:
        print(json.dumps({"file_id": fid, "name": note["name"], "source_pdf": src,
                          "pages": [{"no": r[0], "page": r[1], "index": r[2], "records": r[3],
                                     "points": r[4], "canvas": r[5], "kinds": r[6], "usn": r[7]}
                                    for r in rows]}, ensure_ascii=False, indent=1))
        return
    print("笔记 %s  (%s)" % (note["name"], fid))
    print("源 PDF: %s" % (src or "无"))
    print("\n%-3s %-26s %5s %5s %7s %11s %s" % ("页", "页 id", "index", "条数", "点数", "画布", "记录类型"))
    for r in rows:
        kinds = " ".join("%s×%d" % (k, v) for k, v in sorted(r[6].items()))
        print("%-3d %-26s %5s %5d %7d %11s %s" % (r[0], r[1], r[2], r[3], r[4], r[5], kinds))
    print("\n合计 %d 条 / %d 点" % (tot_r, tot_p))


def cmd_dump(a):
    note = C.find_note(a.note, a.root)
    if a.page:
        pg = C.read_page(C.page_order(note["file_id"], a.root)[a.page - 1])
        out = M.page_model(pg)
        out = {"format": M.FORMAT, "note": {"file_id": note["file_id"], "name": note["name"]},
               "pages": [out]}
    else:
        out = M.note_model(note["file_id"], a.root)
    txt = json.dumps(out, ensure_ascii=False, indent=a.indent) if a.indent else json.dumps(out, ensure_ascii=False)
    if a.out:
        open(a.out, "w").write(txt)
        print("已写出 %s（%s，%d 页 / %d 个对象）" % (a.out, _fmt_size(len(txt)), len(out["pages"]),
                                                sum(len(p["objects"]) for p in out["pages"])))
    else:
        print(txt)


def cmd_export(a):
    note = C.find_note(a.note, a.root)
    fid, src, items = _note_frames(note, a.root, a.page)
    if not items:
        print("没有可导出的页")
        return
    out = a.out or os.path.join(os.getcwd(), "%s-export" % fid)
    os.makedirs(out, exist_ok=True)
    os.makedirs(a.tmp, exist_ok=True)
    if a.format == "json":
        M.dump(fid, out=os.path.join(out, fid + ".json"), root=a.root, indent=a.indent or 1)
        print("JSON → %s" % os.path.join(out, fid + ".json"))
        return
    frames = [(no, _page_of(p, a.root)) for no, p in items]
    if a.format == "png":
        for no, page in frames:
            img = RD.render_page(page, page_no=no, bg_pdf=src, tmp_dir=a.tmp, gain=a.gain, mode=a.canvas)
            f = os.path.join(out, "%s__p%d.png" % (fid, no))
            img.save(f)
            print("PNG  %s  %s" % (_fmt_size(os.path.getsize(f)), f))
    elif a.format == "pdf":
        imgs = [RD.render_page(page, page_no=no, bg_pdf=src, tmp_dir=a.tmp, gain=a.gain, mode=a.canvas)
                for no, page in frames]
        f = os.path.join(out, fid + ".pdf")
        imgs[0].save(f, save_all=True, append_images=imgs[1:], resolution=150.0)
        print("栅格 PDF  %s  %d 页  %s%s" % (_fmt_size(os.path.getsize(f)), len(imgs), f,
                                            "（含源 PDF 底图）" if src else ""))
    elif a.format == "svg":
        for no, page in frames:
            f = os.path.join(out, "%s__p%d.svg" % (fid, no))
            open(f, "w").write(RD.svg(page, has_bg=bool(src), title="%s p%d" % (fid, no), gain=a.gain, mode=a.canvas))
            print("SVG  %s  %s" % (_fmt_size(os.path.getsize(f)), f))
    elif a.format == "vpdf":
        data = RD.pdf([(no, page, bool(src)) for no, page in frames], bg_pdf=src, tmp_dir=a.tmp,
                      gain=a.gain, mode=a.canvas)
        f = os.path.join(out, fid + ".vpdf.pdf")
        open(f, "wb").write(data)
        print("矢量 PDF  %s  %s" % (_fmt_size(len(data)), f))
    elif a.format == "html":
        f = os.path.join(out, "%s.html" % fid)
        open(f, "w").write(RD.html([("%s p%d" % (fid, no), page, bool(src)) for no, page in frames],
                                   gain=a.gain, mode=a.canvas))
        print("HTML  %s  %s" % (_fmt_size(os.path.getsize(f)), f))


def cmd_build(a):
    spec = json.load(open(a.spec))
    if "pages" not in spec:
        raise SystemExit("spec 里缺少 pages")
    tpl = W.templates(a.root)
    need = sorted({o.get("tool") or R.KIND2TOOL.get(o.get("kind")) for p in spec["pages"]
                   for o in (p.get("objects") or [])})
    missing = [t for t in need if t not in ("FunPen", None) and t not in tpl]
    if missing:
        raise SystemExit("缺少这些类型的前缀模板（先在 App 里画一条）：%s" % ", ".join(missing))
    out = a.out or os.path.join(a.tmp, "stage-%d" % int(time.time() * 1000))
    man = W.build(spec, out, name=a.name, root=a.root, tpl=tpl)
    sc = man["self_check"]
    print("已造到 %s" % man["dir"])
    print("  %s  name=%r  页=%d  对象=%d  类型=%s" % (man["file_id"], man["name"], sc["pages"],
                                                    sc["objects"], ",".join(man["types"])))
    print("  自检: %s%s" % ("通过" if sc["ok"] else "失败", "" if sc["ok"] else "  " + "; ".join(sc["errors"])))
    if not a.push:
        print("\n（未推送。加 --push 推到网盘，需要写权限 → 用 sudo 跑）")
        return
    W.push(man, root=a.root, backup_dir=a.backup or a.tmp)
    print("已推送到 %s（pathinfo 已加条目，原文件已备份到 %s）" % (C.notebook_dir(a.root), a.backup or a.tmp))
    print("提示：服务端删除会被 App 同步回来，要删就在 App 里删。")


def cmd_verify(a):
    only = getattr(a, "note", None)
    notes = C.list_notes(a.root) if (getattr(a, "all", False) or not only) else [C.find_note(only, a.root)]
    tot = ok = 0
    bad = []
    for n in notes:
        for p in C.page_paths(n["file_id"], a.root):
            pg = C.read_page(p)
            tot += 1
            if C.page_bytes(pg, force=True) == pg["raw"]:
                ok += 1
            else:
                bad.append(p)
            cov = sum(r["size"] for r in pg["records"])
            if cov != len(pg["region_raw"]):
                bad.append(p + "（索引覆盖不全）")
    print("字节级回合: %d/%d %s" % (ok, tot, "✓ 全部无损" if ok == tot and not bad else "✗"))
    for b in bad:
        print("  " + b)
    return 0 if ok == tot and not bad else 1


def cmd_remove(a):
    note = C.find_note(a.note, a.root)
    if not a.yes:
        print("将移除 %s（%s）。加 --yes 确认。" % (note["file_id"], note["name"]))
        return
    r = W.remove(note["file_id"], root=a.root, backup_dir=a.backup or a.tmp)
    print("已移除 %s（pathinfo 清掉 %d 条）" % (r["file_id"], r["pathinfo_entries_removed"]))


def cmd_doctor(a):
    from PIL import Image  # noqa: F401
    print("根目录      %s" % C.notebook_dir(a.root))
    d = C.notebook_dir(a.root)
    print("  可读      %s" % os.access(d, os.R_OK))
    print("  可写      %s%s" % (os.access(d, os.W_OK), "" if os.access(d, os.W_OK)
                                else "  ← 写入需 sudo"))
    print("Pillow      ✓")
    print("pdftoppm    %s（底图与栅格 PDF 需要）" % ("✓" if RD.HAS_PDFTOOLS else "✗ 未安装 poppler-utils"))
    notes = C.list_notes(a.root)
    print("笔记本      %d 篇 / %d 页" % (len(notes), sum(n["pages"] for n in notes)))
    tpl = W.templates(a.root)
    print("前缀模板    %s" % ", ".join(sorted(tpl)) or "（无）")
    print("可写形状类型 %s" % ", ".join(sorted(t for t in tpl if t != "FunPen")))
    return cmd_verify(a)


# ---------------------------------------------------------------- 入口
def main(argv=None):
    p = argparse.ArgumentParser(prog="tn", description="享做笔记（TouchNotes）导入导出工具")
    p.add_argument("--root", default=DEFAULT_ROOT, help="网盘 touchnotes 根目录")
    p.add_argument("--tmp", default=DEFAULT_TMP, help="临时目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="列出所有笔记")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("info", help="一篇笔记的概要（每页条数/点数/类型）")
    s.add_argument("note")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("dump", help="导出规范化 JSON（导入导出的公共中间格式）")
    s.add_argument("note")
    s.add_argument("-o", "--out")
    s.add_argument("-p", "--page", type=int)
    s.add_argument("--indent", type=int, default=0)
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("export", help="导出成品：png / pdf（栅格，含源 PDF 底图）/ svg / html / vpdf（矢量）")
    s.add_argument("note")
    s.add_argument("-f", "--format", default="pdf",
                   choices=["png", "pdf", "svg", "html", "vpdf", "json"])
    s.add_argument("-o", "--out")
    s.add_argument("-p", "--page", type=int)
    s.add_argument("--gain", type=float, default=1.0, help="笔宽整体倍率")
    s.add_argument("--canvas", default="auto", choices=["auto", "ink", "declared"],
                   help="画布：auto=有底图用声明尺寸/纯笔迹用墨迹包围盒（默认）；ink=一律用墨迹包围盒；declared=一律用笔记声明尺寸")
    s.add_argument("--indent", type=int, default=0)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("build", help="从规范化 JSON 造一篇笔记（--push 推到网盘）")
    s.add_argument("spec")
    s.add_argument("--name")
    s.add_argument("--out", help="本地暂存目录")
    s.add_argument("--push", action="store_true")
    s.add_argument("--backup", help="pathinfo 备份目录")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("verify", help="字节级回合自检")
    s.add_argument("note", nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("remove", help="移除一篇笔记（清 pathinfo；服务端删除会被 App 同步回来）")
    s.add_argument("note")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--backup")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("doctor", help="环境自检（目录/依赖/模板/无损回合）")
    s.set_defaults(func=cmd_doctor)

    a = p.parse_args(argv)
    rc = a.func(a)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
