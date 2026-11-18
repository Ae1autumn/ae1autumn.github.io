#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 GitHub Issues 同步成本地 Markdown + data.jsonc 元数据。

工作方式
--------
1. 用 GitHub REST API 拉取指定仓库的 issues（自动分页）。
2. 用 ETag 做增量：内容没变时 GitHub 返回 304，不重复下载，也不消耗配额。
3. 每个 issue 生成一个 Markdown 文件（默认 posts/issue-<编号>.md）。
4. issue 被删除（或关闭，且 --state=open）时，删除对应的 .md。
5. 把元数据写进 data.jsonc 的 posts 数组；其余配置和注释保持不动。

Issue 正文里可以放这些隐藏标记（GitHub 页面上不显示）：

    <!-- slug: my-post-slug -->
    <!-- excerpt: 显示在首页卡片上的一句话简介 -->
    <!-- date: 2025-11-04 -->
    <!-- pin: true -->

没有 excerpt 标记时，自动取正文第一段（去掉 Markdown、截断到 120 字）。
标签（labels）会变成 tags；pin / pinned 标签等价于 pin: true。

用法
----
    export GITHUB_TOKEN=ghp_xxx          # 也可以放 GH_TOKEN
    python3 blog/sync_issues.py --repo owner/repo --label post

常用参数
--------
    --state open|all      只同步 open（默认）还是全部 issue
    --prune-unmanaged     删除 data.jsonc 里不由本脚本管理的旧文章（默认保留）
    --full                忽略 ETag，强制整页重新拉取
    --dry-run             只打印将要做的改动，不写文件

只依赖 Python 标准库，Python 3.8+。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_API_BASE = "https://api.github.com"

MARK_START = "// >>> issue-sync:posts:start"
MARK_END = "// <<< issue-sync:posts:end"

DEFAULT_DATA = """{
  "posts": [
    // >>> issue-sync:posts:start
    // <<< issue-sync:posts:end
  ],

  // ═══ 阅读量（GoatCounter）═══
  // 1. 注册站点，拿到 xxx.goatcounter.com 里的 code
  // 2. Settings → 开启 "Allow adding visitor counts on your website"
  // 3. 把 code 填到下面；留空则阅读量自动隐藏，其他功能不受影响
  // 计数路径统一为 pathPrefix + slug，与 openPost 里发送的虚拟 pageview 保持一致
  "goatcounter": {
    "code": "",
    "pathPrefix": "/post/",
    "cacheKey": "ae_views_v1",
    "cacheTTL": 1800000 // 30 分钟（30 * 60 * 1000）
  },

  // ═══ 评论（giscus）═══
  // 在 https://giscus.app 选好仓库和 category 后，把 repoId / categoryId 填进来。
  // 前提：仓库开启 Discussions 并安装 giscus app。
  // repoId 或 categoryId 为空、脚本加载失败、giscus 返回错误时，回退到占位符。
  "giscus": {
    "repo": "Ae1autumn/Temps",
    "repoId": "",
    "category": "Comments",
    "categoryId": "",
    "mapping": "specific",
    "strict": "1",
    "reactionsEnabled": "1",
    "inputPosition": "bottom",
    "lang": "zh-CN"
  }
}
"""


# ─────────────────────────── 小工具 ───────────────────────────

def log(msg):
    print(msg, flush=True)


def fail(msg):
    raise SystemExit(msg)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path):
    return path.read_text(encoding="utf-8-sig")


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def strip_jsonc(src):
    """去掉 JSONC 的行注释、块注释和尾随逗号，字符串内容原样保留。"""
    out = []
    i = 0
    n = len(src)
    in_str = False
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_str:
            out.append(ch)
            if ch == "\\":
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        if ch == ",":
            # 逗号后面若只有空白/注释，再后面是 } 或 ]，说明是尾随逗号
            j = i + 1
            while j < n:
                c = src[j]
                if c in " \t\r\n":
                    j += 1
                    continue
                if c == "/" and j + 1 < n and src[j + 1] == "/":
                    while j < n and src[j] != "\n":
                        j += 1
                    continue
                if c == "/" and j + 1 < n and src[j + 1] == "*":
                    j += 2
                    while j < n and not (src[j] == "*" and j + 1 < n and src[j + 1] == "/"):
                        j += 1
                    j += 2
                    continue
                break
            if j < n and src[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_jsonc(text):
    return json.loads(strip_jsonc(text))


# ───────────────────── data.jsonc 的定位与改写 ─────────────────────

def find_posts_array(text):
    """返回 "posts": [ 的 '[' 位置和与之匹配的 ']' 位置。"""
    m = re.search(r'"posts"\s*:\s*\[', text)
    if not m:
        fail('data.jsonc 里找不到 "posts": [ ... ]')
    open_pos = text.index("[", m.start())
    depth = 1
    i = open_pos + 1
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return open_pos, i
        i += 1
    fail('data.jsonc 的 "posts" 数组没有闭合')


def locate_markers(text):
    start = text.find(MARK_START)
    end = text.find(MARK_END)
    if start >= 0 and end >= 0 and end > start:
        return start, end
    if start >= 0 or end >= 0:
        fail("data.jsonc 里的 issue-sync 标记不完整，请检查")
    return None


def ensure_markers(text):
    """没有标记时，在 posts 数组里插入一对标记。"""
    if locate_markers(text) is not None:
        return text
    open_pos, close_pos = find_posts_array(text)
    line_start = text.rfind("\n", 0, open_pos) + 1
    prefix = text[line_start:open_pos]
    key_indent = prefix[: len(prefix) - len(prefix.lstrip())]
    elem_indent = key_indent + "  "
    block = (
        "[\n"
        + elem_indent + MARK_START + "\n"
        + elem_indent + MARK_END + "\n"
        + key_indent + "]"
    )
    return text[:open_pos] + block + text[close_pos + 1:]


def render_posts(posts, indent):
    if not posts:
        return ""
    chunks = []
    for post in posts:
        raw = json.dumps(post, ensure_ascii=False, indent=2)
        chunks.append("\n".join(indent + line for line in raw.splitlines()))
    return ",\n".join(chunks) + "\n"


POST_KEY_ORDER = ("slug", "title", "date", "tags", "excerpt", "src", "pin")


def normalize_post(post):
    """固定字段顺序，避免状态文件 sort_keys 后导致 data.jsonc 反复抖动。"""
    out = {}
    for key in POST_KEY_ORDER:
        if key in post:
            out[key] = post[key]
    for key, value in post.items():
        if key not in out:
            out[key] = value
    return out


def replace_posts(text, posts):
    text = ensure_markers(text)
    start, end = locate_markers(text)
    start_line_end = text.find("\n", start)
    if start_line_end < 0:
        fail("data.jsonc 的 posts 起始标记后面缺少换行")
    end_line_start = text.rfind("\n", 0, end) + 1
    marker_line_start = text.rfind("\n", 0, start) + 1
    indent = text[marker_line_start:start]
    new_text = text[: start_line_end + 1] + render_posts(posts, indent) + text[end_line_start:]
    # 写盘前先验证，避免生成坏文件
    try:
        json.loads(strip_jsonc(new_text))
    except json.JSONDecodeError as exc:
        fail(f"生成的 data.jsonc 不是合法 JSONC：{exc}")
    return new_text


def load_existing_posts(text):
    try:
        data = parse_jsonc(text)
    except json.JSONDecodeError as exc:
        fail(f"data.jsonc 解析失败：{exc}")
    posts = data.get("posts") or []
    if not isinstance(posts, list):
        fail('data.jsonc 里的 "posts" 必须是数组')
    return posts


# ─────────────────────────── Issue → 元数据 ───────────────────────────

def slugify(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    value = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def marker(body, name):
    m = re.search(
        r"<!--\s*" + re.escape(name) + r"\s*:\s*([\s\S]*?)-->",
        body or "",
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


META_MARKER_RE = re.compile(
    r"[ \t]*<!--\s*(?:slug|excerpt|date|pin)\s*:[\s\S]*?-->[ \t]*(?:\r?\n)?",
    re.IGNORECASE,
)


def clean_body(body):
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = META_MARKER_RE.sub("", text)
    return text.strip() + "\n" if text.strip() else ""


def auto_excerpt(body, max_chars=120):
    s = body or ""
    s = re.sub(r"```[\s\S]*?```", "", s)
    s = re.sub(r"~~~[\s\S]*?~~~", "", s)
    s = re.sub(r"`[^`\n]*`", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*([-*_]\s*){3,}$", "", s, flags=re.MULTILINE)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[*_~]", "", s)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
    text = ""
    for para in paragraphs:
        text = (text + " " + para).strip()
        if len(text) >= 60:
            break
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def issue_to_post(issue, args, state, data_dir, posts_dir):
    """把一个 issue 转换成 (元数据 dict, Markdown 文本, 文件相对路径)。"""
    number = int(issue["number"])
    body = issue.get("body") or ""
    labels = [l.get("name") if isinstance(l, dict) else str(l) for l in (issue.get("labels") or [])]
    labels = [l for l in labels if l]

    title = str(issue.get("title") or "").strip() or f"Issue #{number}"

    slug = marker(body, "slug")
    slug = slugify(slug) if slug else ""
    if not slug:
        slug = f"issue-{number}"

    # slug 冲突时加编号，保证文件名唯一
    owner = {info.get("slug"): int(num) for num, info in state["issues"].items() if info.get("slug")}
    if owner.get(slug) not in (None, number):
        slug = f"{slug}-{number}"
    base_slug, n = slug, 2
    while owner.get(slug) not in (None, number):
        slug = f"{base_slug}-{n}"
        n += 1

    date = marker(body, "date")
    if not (date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)):
        date = str(issue.get("created_at") or "")[:10]

    pin_text = (marker(body, "pin") or "").lower()
    pin = pin_text in ("true", "1", "yes", "on")
    pin = pin or any(l.lower() in ("pin", "pinned") for l in labels)

    post_label = (args.label or "").lower()
    tags = sorted(
        {l for l in labels if l.lower() != post_label and l.lower() not in ("pin", "pinned")},
        key=str.lower,
    )

    md_text = clean_body(body)
    excerpt_marker = marker(body, "excerpt")
    excerpt = (excerpt_marker or "").strip() or auto_excerpt(md_text, args.excerpt_length)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()

    rel = os.path.relpath(posts_dir / f"{slug}.md", data_dir).replace(os.sep, "/")
    src = rel if rel.startswith(".") else "./" + rel

    meta = {
        "slug": slug,
        "title": title,
        "date": date,
        "tags": tags,
        "excerpt": excerpt,
        "src": src,
    }
    if pin:
        meta["pin"] = True
    return meta, md_text, rel


# ─────────────────────────── 文件删除 ───────────────────────────

def remove_managed_file(rel_path, base_dir, posts_dir, dry_run=False):
    """只允许删除 posts 目录下的 .md，避免误删其它文件。"""
    if not rel_path:
        return
    target = (base_dir / rel_path).resolve()
    root = posts_dir.resolve()
    if target.suffix.lower() != ".md":
        log(f"  跳过删除（不是 .md）：{target}")
        return
    if target.parent != root and root not in target.parents:
        log(f"  跳过删除（不在 posts 目录）：{target}")
        return
    if not target.exists():
        return
    if dry_run:
        log(f"  [dry-run] 删除 {target}")
    else:
        target.unlink()
        log(f"  删除 {target}")


# ─────────────────────────── GitHub API ───────────────────────────

def token_from_env():
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def detect_repo():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(SCRIPT_DIR),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else None


def api_get(url, token, etag=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "issue-sync-script",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if etag:
        headers["If-None-Match"] = etag

    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200, json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, None, exc.headers
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(1 + attempt)
                continue
            if exc.code in (403, 429):
                remaining = exc.headers.get("X-RateLimit-Remaining")
                reset = exc.headers.get("X-RateLimit-Reset")
                detail = exc.read().decode("utf-8", "replace")[:300]
                msg = f"GitHub API {exc.code}"
                if remaining == "0" and reset:
                    when = datetime.fromtimestamp(int(reset), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
                    msg += f"：API 配额已用完，{when} 重置"
                else:
                    msg += "：" + detail
                fail(msg)
            fail(f"GitHub API {exc.code}：{exc.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            fail(f"网络错误：{exc}")
    fail(f"请求失败：{last_error}")


def issue_page_url(api_base, repo, args, page):
    params = {
        "state": args.state,
        "per_page": "100",
        "page": str(page),
        "sort": "created",
        "direction": "desc",
    }
    if args.label:
        params["labels"] = args.label
    return f"{api_base}/repos/{repo}/issues?" + urllib.parse.urlencode(params)


def has_next_page(headers):
    return 'rel="next"' in (headers.get("Link") or "")


def fetch_issues(api_base, repo, args, token, state):
    """返回 (当前 issue 编号集合, 本页新拉到的 issue 数据 dict)。"""
    page = 1
    current = set()
    pending = {}
    seen_urls = set()

    while True:
        url = issue_page_url(api_base, repo, args, page)
        seen_urls.add(url)
        cached = state["pages"].get(url) or {}
        etag = None if args.full else cached.get("etag")
        status, data, headers = api_get(url, token, etag)

        if status == 304:
            if not cached or "numbers" not in cached:
                fail("GitHub 返回 304，但本地没有对应缓存；请加 --full 重试")
            nums = cached.get("numbers") or []
            current.update(nums)
            log(f"  第 {page} 页：304 未变化（{len(nums)} 篇）")
            next_url = issue_page_url(api_base, repo, args, page + 1)
            if next_url not in state["pages"]:
                break
            page += 1
            continue

        nums = []
        for issue in data:
            if "pull_request" in issue:  # 列表接口会混入 PR
                continue
            number = int(issue["number"])
            nums.append(number)
            current.add(number)
            pending[number] = issue
        state["pages"][url] = {"etag": headers.get("ETag"), "numbers": nums}
        log(f"  第 {page} 页：{len(data)} 条，其中 issue {len(nums)} 篇")

        if not data or not has_next_page(headers):
            break
        page += 1

    # 页数变少时，清掉多余的缓存页
    state["pages"] = {u: v for u, v in state["pages"].items() if u in seen_urls}
    return current, pending


# ─────────────────────────── 状态文件 ───────────────────────────

def load_state(path, repo):
    if path.exists():
        try:
            state = json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            fail(f"状态文件解析失败：{path}：{exc}")
        if state.get("repo") and state["repo"] != repo:
            fail(
                f"状态文件属于 {state['repo']}，与 --repo {repo} 不一致；"
                "请换一个 --state-file，避免误删另一个仓库的文件"
            )
    else:
        state = {}
    state.setdefault("version", 1)
    state.setdefault("repo", repo)
    state.setdefault("pages", {})
    state.setdefault("issues", {})
    return state


def save_state(path, state, old_text, dry_run=False):
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if text == old_text:
        return False
    if dry_run:
        log(f"  [dry-run] 更新状态文件 {path}")
        return True
    atomic_write(path, text)
    return True


# ─────────────────────────── 主流程 ───────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="把 GitHub Issues 同步成 Markdown 文件和 data.jsonc 元数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  export GITHUB_TOKEN=ghp_xxx\n"
            "  python3 blog/sync_issues.py --repo owner/repo --label post\n"
        ),
    )
    parser.add_argument("--repo", default=detect_repo(), help="owner/repo，默认从 git remote 推断")
    parser.add_argument("--label", default="", help="只同步带这个标签的 issue；不填则同步全部")
    parser.add_argument("--state", choices=["open", "all"], default="open", help="同步 open 还是全部 issue")
    parser.add_argument("--token", default=token_from_env(), help="GitHub Token，默认读 GITHUB_TOKEN / GH_TOKEN")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="API 地址，GitHub Enterprise 可改")
    parser.add_argument("--data-file", default=str(SCRIPT_DIR / "data.jsonc"), help="data.jsonc 路径")
    parser.add_argument("--posts-dir", default=str(SCRIPT_DIR / "posts"), help="Markdown 输出目录")
    parser.add_argument("--state-file", default=str(SCRIPT_DIR / ".issues-sync.json"), help="增量状态文件")
    parser.add_argument("--excerpt-length", type=int, default=120, help="自动简介的最大字数")
    parser.add_argument("--prune-unmanaged", action="store_true", help="删除 posts 里不是由 issues 管理的旧文章")
    parser.add_argument("--full", action="store_true", help="忽略 ETag，强制重新拉取")
    parser.add_argument("--dry-run", action="store_true", help="只打印改动，不写文件")
    return parser


def main():
    args = build_parser().parse_args()
    if not args.repo:
        fail("缺少 --repo；例如 --repo Ae1autumn/Temps")
    args.repo = re.sub(r"\.git$", "", args.repo.strip())
    args.label = (args.label or "").strip()
    args.api_base = args.api_base.rstrip("/")

    data_file = Path(args.data_file).resolve()
    posts_dir = Path(args.posts_dir).resolve()
    state_file = Path(args.state_file).resolve()
    data_dir = data_file.parent

    if not args.token:
        log("提示：没有 GITHUB_TOKEN，未认证请求限流为 60 次/小时")

    log(f"仓库：{args.repo}")
    log(f"数据：{data_file}")
    log(f"文章目录：{posts_dir}")

    state = load_state(state_file, args.repo)
    old_state_text = read_text(state_file) if state_file.exists() else ""
    if state.get("label") is not None and state.get("label") != args.label:
        log("提示：--label 与上次不同，不在新列表里的旧 issue 会被删除")
    if state.get("api_state") is not None and state.get("api_state") != args.state:
        log("提示：--state 与上次不同，状态不同的 issue 会被增删")
    managed_slugs = {info.get("slug") for info in state["issues"].values() if info.get("slug")}

    log("拉取 issues：")
    current, pending = fetch_issues(args.api_base, args.repo, args, args.token, state)

    # 1) 删除已消失的 issue 对应的文件
    deleted_numbers = sorted(set(int(n) for n in state["issues"]) - current)
    for number in deleted_numbers:
        info = state["issues"].pop(str(number), None) or {}
        log(f"issue #{number} 已删除/关闭：{info.get('file', '')}")
        remove_managed_file(info.get("file"), data_dir, posts_dir, args.dry_run)

    # 2) 新增 / 更新
    written = unchanged = 0
    for number in sorted(pending, reverse=True):
        issue = pending[number]
        meta, md_text, rel = issue_to_post(issue, args, state, data_dir, posts_dir)
        managed_slugs.add(meta["slug"])

        target = data_dir / rel
        body_hash = sha256_text(md_text)
        old = state["issues"].get(str(number)) or {}
        need_write = (
            old.get("file") != rel
            or old.get("body_sha256") != body_hash
            or not target.exists()
        )

        if need_write:
            if args.dry_run:
                log(f"  [dry-run] 写入 {target}")
            else:
                atomic_write(target, md_text)
                log(f"  写入 {target}")
            written += 1
        else:
            unchanged += 1

        # slug 改了：删掉旧文件
        old_file = old.get("file")
        if old_file and old_file != rel:
            remove_managed_file(old_file, data_dir, posts_dir, args.dry_run)

        state["issues"][str(number)] = {
            "file": rel,
            "slug": meta["slug"],
            "body_sha256": body_hash,
            "updated_at": issue.get("updated_at"),
            "meta": meta,
        }

    # 3) 合并 posts：issues 生成的 + 手工保留的
    managed_posts = []
    for number in current:
        info = state["issues"].get(str(number))
        if info and info.get("meta"):
            managed_posts.append(normalize_post(info["meta"]))
        else:
            log(f"  警告：issue #{number} 缺少本地元数据，本次跳过；可加 --full 重新拉取")

    if data_file.exists():
        data_text = read_text(data_file)
        existing_posts = load_existing_posts(data_text)
    else:
        data_text = DEFAULT_DATA
        existing_posts = []

    if args.prune_unmanaged:
        unmanaged = []
    else:
        unmanaged = [p for p in existing_posts if p.get("slug") not in managed_slugs]

    combined = []
    seen = set()
    for post in managed_posts + unmanaged:
        slug = post.get("slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        combined.append(post)

    # 置顶优先，其余按日期从新到旧；同日期按 slug，保证输出稳定
    combined.sort(
        key=lambda p: (
            1 if p.get("pin") else 0,
            str(p.get("date") or ""),
            str(p.get("slug") or ""),
        ),
        reverse=True,
    )

    new_data_text = replace_posts(data_text, combined)
    data_changed = new_data_text != data_text or not data_file.exists()
    if data_changed:
        if args.dry_run:
            log(f"  [dry-run] 更新 {data_file}")
        else:
            atomic_write(data_file, new_data_text)
            log(f"  更新 {data_file}")
    else:
        log("  data.jsonc 无变化")

    # 4) 保存状态
    state["label"] = args.label
    state["api_state"] = args.state
    state_changed = save_state(state_file, state, old_state_text, args.dry_run)

    suffix = "（dry-run）" if args.dry_run else ""
    log(
        "完成：新增/更新文件 {}{}，未变 {}，删除 issue {}，jsonc {}，state {}".format(
            written,
            suffix,
            unchanged,
            len(deleted_numbers),
            ("已更新" if data_changed else "无变化") + suffix,
            ("已更新" if state_changed else "无变化") + suffix,
        )
    )


if __name__ == "__main__":
    main()
