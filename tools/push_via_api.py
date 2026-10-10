#!/usr/bin/env python3
"""从「git 端口被墙、API 通」的机器往 GitHub 推送。

背景：本机直连 github.com 的 https git 端点会 SSL 超时（中国大陆常见），
但 api.github.com 可达。所以这里用 GitHub 的 Git Data API 把当前仓库的
HEAD 快照推上去（blob → tree → commit → 更新 ref），不需要 git 能连上 GitHub。

用法::

    export GITHUB_TOKEN=ghp_...          # 或写在 ~/.hermes/.env 里（脚本会自己找）
    python3 tools/push_via_api.py                     # 推到 origin 指向的 GitHub 仓库
    python3 tools/push_via_api.py --repo RealMikeDuke/touchnotes-tool --branch main
    python3 tools/push_via_api.py --dry-run

只推 git **已跟踪**（`git ls-files`）的文件 —— 没提交的东西不会被推上去。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def token():
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t.strip()
    for p in (os.path.join(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")), ".env"),
              os.path.expanduser("~/.config/gh/token")):
        if os.path.exists(p):
            for line in open(p, errors="ignore"):
                if line.startswith(("GITHUB_TOKEN=", "GH_TOKEN=")):
                    return line.split("=", 1)[1].strip()
    sys.exit("找不到 token：设 GITHUB_TOKEN 或写进 ~/.hermes/.env")


def api(tok, path, method="GET", body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                 "User-Agent": "push-via-api", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True, **kw).stdout


def repo_from_origin():
    url = git("remote", "get-url", "origin").strip()
    if not url:
        return None
    for pat in ("github.com/", "github.com:"):
        if pat in url:
            slug = url.split(pat, 1)[1]
            return slug[:-4] if slug.endswith(".git") else slug
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="OWNER/NAME（默认从 origin 解析）")
    ap.add_argument("--branch", default=None, help="默认取当前分支")
    ap.add_argument("--message", default=None, help="默认取 HEAD 的提交信息")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tok = token()
    slug = a.repo or repo_from_origin()
    if not slug:
        sys.exit("推不出仓库名：用 --repo OWNER/NAME")
    branch = a.branch or git("rev-parse", "--abbrev-ref", "HEAD").strip() or "main"
    msg = a.message if a.message is not None else git("log", "-1", "--format=%B").strip()

    files = [f for f in git("ls-files").split("\n") if f]
    if not files:
        sys.exit("git ls-files 是空的 —— 先 git add / git commit")
    print("  仓库 %s  分支 %s  %d 个已跟踪文件" % (slug, branch, len(files)))

    st, d = api(tok, "/repos/" + slug)
    if st != 200:
        sys.exit("  打不开仓库 %s：HTTP %s %s" % (slug, st, d.get("message")))
    owner = d["owner"]["login"]
    print("  仓库存在，owner=%s，默认分支=%s" % (owner, d.get("default_branch")))

    # 远端当前 HEAD（没有就是初次提交）
    st, d = api(tok, "/repos/%s/git/ref/heads/%s" % (slug, branch))
    parents = [d["object"]["sha"]] if st == 200 else []
    print("  远端 %s 当前: %s" % (branch, parents[0][:10] if parents else "（空仓库）"))

    if a.dry_run:
        print("  --dry-run：到此为止")
        return
    tree = []
    for p in files:
        data = open(p, "rb").read()
        st, d = api(tok, "/repos/%s/git/blobs" % slug, "POST",
                    {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if st == 409:
            sys.exit("  HTTP 409：空仓库不能用 Git Data API。先用 contents API 建首次提交，"
                     "或在有网的机器上 git push 一次：\n"
                     "    curl -X PUT -H \"Authorization: Bearer $GITHUB_TOKEN\" "
                     "https://api.github.com/repos/%s/contents/README.md "
                     "-d '{\"message\":\"init\",\"content\":\"\"}'" % slug)
        if st not in (200, 201):
            sys.exit("  blob %s 失败：HTTP %s %s" % (p, st, d.get("message")))
        tree.append({"path": p, "mode": "100755" if os.access(p, os.X_OK) else "100644",
                     "type": "blob", "sha": d["sha"]})
        print("    blob %-28s %7d B" % (p, len(data)))
    st, d = api(tok, "/repos/%s/git/trees" % slug, "POST", {"tree": tree})
    if st not in (200, 201):
        sys.exit("  tree 失败：HTTP %s %s" % (st, d.get("message")))
    tree_sha = d["sha"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ident = {"name": git("config", "user.name").strip() or owner,
             "email": git("config", "user.email").strip() or "%s@users.noreply.github.com" % owner,
             "date": now}
    st, d = api(tok, "/repos/%s/git/commits" % slug, "POST",
                {"message": msg, "tree": tree_sha, "parents": parents,
                 "author": ident, "committer": ident})
    if st not in (200, 201):
        sys.exit("  commit 失败：HTTP %s %s" % (st, d.get("message")))
    commit = d["sha"]
    if parents:
        st, d = api(tok, "/repos/%s/git/refs/heads/%s" % (slug, branch), "PATCH",
                    {"sha": commit, "force": False})
    else:
        st, d = api(tok, "/repos/%s/git/refs" % slug, "POST",
                    {"ref": "refs/heads/" + branch, "sha": commit})
    if st not in (200, 201):
        sys.exit("  更新 ref 失败：HTTP %s %s" % (st, d.get("message")))
    print("  推送完成 commit=%s" % commit)
    print("  https://github.com/%s/commit/%s" % (slug, commit))
    print("  注意：本地对象库没有这次远端提交（本地 HEAD=%s 与远端不同源）。"
          % git("rev-parse", "HEAD").strip()[:10])
    print("        以后要本地也对齐，在有网的机器上 git fetch 即可。")


if __name__ == "__main__":
    main()
