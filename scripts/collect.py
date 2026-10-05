#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 前沿日报采集器
------------------
从 GitHub / YouTube / 博客 RSS / 论文 / HackerNews 采集 AI、LLM、Harness、Agent
方向的前沿动态，去重 + 相似检测 + DeepSeek 中文摘要，生成 Markdown 日报。

仅使用 Python 标准库，无需 pip install。

环境变量：
  DEEPSEEK_API_KEY   必填才会生成中文摘要（缺失则跳过摘要，仍输出链接）
  DEEPSEEK_BASE_URL  默认 https://api.deepseek.com/v1
  DEEPSEEK_MODEL     默认 deepseek-chat
  GITHUB_TOKEN       可选，提高 GitHub API 配额（Actions 里自动有）
  DRY_RUN=1          可选，跳过 LLM 摘要
"""

import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.json"
STATE_PATH = ROOT / "state" / "seen.json"
DIGEST_DIR = ROOT / "digest"

CST = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DEBUG = os.environ.get("DEBUG") == "1"

# 记录本次访问失败的域名，用于在日报里提醒「某源今天没拿到数据」
FETCH_FAILURES = {}
SOURCE_COUNT = {}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url, timeout=45, retries=3, headers=None):
    """带重试的 GET，返回 bytes；失败返回 None。"""
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/xml, application/xml, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                body = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                elif enc == "deflate":
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = zlib.decompress(body, -zlib.MAX_WBITS)
                return body
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    host = urllib.parse.urlparse(url).netloc
    FETCH_FAILURES[host] = FETCH_FAILURES.get(host, 0) + 1
    log(f"  [warn] GET 失败 {url} -> {type(last).__name__}: {last}")
    return None


def http_post_json(url, payload, headers=None, timeout=180, retries=3):
    hdrs = {"Content-Type": "application/json", "User-Agent": UA}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            log(f"  [warn] POST {url} -> HTTP {e.code} {detail}")
            if e.code in (400, 401, 403, 404):
                return None
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    log(f"  [warn] POST 失败 {url} -> {type(last).__name__}: {last}")
    return None


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_dt(s):
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


# 修掉 RSS 里常见的非法 XML：裸露的 &、控制字符、HTML 式自闭合标签。CDATA 段内部不动。
_BARE_AMP = re.compile(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)")
_CTRL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CDATA_SPLIT = re.compile(r"(<!\[CDATA\[.*?\]\])", re.S)
_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_VOID = re.compile(r"<(br|hr|img|input|source|area|base|col|embed|param|track|wbr)\b([^>]*?)/?>", re.I)


def sanitize_xml(raw):
    s = raw.decode("utf-8", "replace")
    s = _CTRL_CHAR.sub(" ", s)
    s = _SCRIPT.sub(" ", s)
    s = _VOID.sub(lambda m: f"<{m.group(1)}{m.group(2)}/>", s)
    parts = _CDATA_SPLIT.split(s)
    return "".join(
        p if p.startswith("<![CDATA[") else _BARE_AMP.sub("&amp;", p) for p in parts
    )


def find_text(node, *names):
    """在 node 的子孙里找第一个名字匹配的非空文本。"""
    for child in node.iter():
        if local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def find_attr(node, *names):
    for child in node.iter():
        if local_name(child.tag) in names:
            for k, v in child.attrib.items():
                if local_name(k) in ("href", "url") and v:
                    return v
    return None


# --------------------------------------------------------------------------
# Feed 解析
# --------------------------------------------------------------------------

def parse_feed(raw, default_link=None):
    """解析 RSS / Atom，返回 [{title, link, published, summary}]。

    先试 XML 解析；若失败或没解析出条目，就用正则兜底，保证坏 feed 也能用。
    """
    if not raw:
        return []
    out = []
    try:
        root = ET.fromstring(sanitize_xml(raw))
        entries = [n for n in root.iter() if local_name(n.tag) in ("entry", "item")]
        for e in entries:
            title = strip_tags(find_text(e, "title") or "")
            if not title:
                continue
            link = None
            for child in e.iter():
                ln = local_name(child.tag)
                if ln == "link":
                    rel = child.attrib.get("rel", "alternate")
                    href = child.attrib.get("href")
                    if href and rel == "alternate":
                        link = href
                        break
                    if href and not link:
                        link = href
                    # RSS 的 <link>http://…</link> 写法
                    txt = (child.text or "").strip()
                    if not href and txt.startswith("http") and not link:
                        link = txt
                elif ln == "guid" and child.text and child.text.strip().startswith("http"):
                    link = link or child.text.strip()
            if not link:
                link = find_attr(e, "link") or default_link
            published = find_text(e, "published", "updated", "pubDate", "date")
            summary = strip_tags(
                find_text(e, "summary", "description", "content", "encoded") or ""
            )
            out.append(_mk_item(title, link, published, summary))
    except ET.ParseError as e:
        log(f"  [warn] XML 解析失败({e})，改用正则兜底")
        out = []

    if not out:
        out = parse_feed_regex(raw, default_link)
    return out


def _mk_item(title, link, published, summary):
    if len(summary) > 500:
        summary = summary[:500] + "…"
    return {
        "title": title,
        "link": link,
        "published": published,
        "dt": parse_dt(published),
        "summary": summary,
    }


_RE_ITEM = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def _re_tag(block, name):
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}\s*>", block, re.S | re.I)
    return strip_tags(m.group(1)) if m else ""


def parse_feed_regex(raw, default_link=None):
    """正则兜底：无视 XML 合法性，直接抠出条目。"""
    s = raw.decode("utf-8", "replace")
    out = []
    for m in _RE_ITEM.finditer(s):
        block = m.group(2)
        title = _re_tag(block, "title")
        if not title:
            continue
        lm = re.search(r'<link\b[^>]*href="([^"]+)"', block, re.I)
        if lm:
            link = lm.group(1)
        else:
            link = _re_tag(block, "link") or _re_tag(block, "guid")
        published = (
            _re_tag(block, "pubDate")
            or _re_tag(block, "published")
            or _re_tag(block, "updated")
            or _re_tag(block, "date")
        )
        summary = (
            _re_tag(block, "description")
            or _re_tag(block, "summary")
            or _re_tag(block, "content:encoded")
            or _re_tag(block, "content")
        )
        out.append(_mk_item(title, link or default_link, published, summary))
    if out:
        log(f"  [info] 正则兜底成功，解析出 {len(out)} 条")
    return out


# --------------------------------------------------------------------------
# 采集器
# --------------------------------------------------------------------------

def collect_github(cfg, now):
    if not cfg.get("enabled"):
        return []
    log("[github] 搜索热门仓库 …")
    days = int(cfg.get("pushed_within_days", 7))
    created_days = int(cfg.get("created_within_days", 45))
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    created_since = (now - timedelta(days=created_days)).strftime("%Y-%m-%d")
    per_page = int(cfg.get("per_page", 8))
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    seen_repos = {}
    queries = cfg.get("queries", [])
    for i, qc in enumerate(queries):
        if isinstance(qc, str):
            qc = {"q": qc}
        min_stars = int(qc.get("min_stars", 20))
        cdays = int(qc.get("created_within_days", created_days))
        csince = (now - timedelta(days=cdays)).strftime("%Y-%m-%d")
        full_q = (
            f"{qc['q']} created:>{csince} pushed:>{since} stars:>{min_stars}"
        )
        url = (
            "https://api.github.com/search/repositories?q="
            + urllib.parse.quote(full_q)
            + f"&sort=stars&order=desc&per_page={per_page}"
        )
        raw = http_get(url, headers=headers)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"  [warn] JSON 解析失败: {e}")
            continue
        for r in data.get("items", []):
            name = r.get("full_name")
            if not name or name in seen_repos:
                continue
            seen_repos[name] = {
                "source": "github",
                "source_label": "GitHub",
                "title": name,
                "link": r.get("html_url"),
                "dt": parse_dt(r.get("created_at") or r.get("pushed_at")),
                "summary": strip_tags(r.get("description") or ""),
                "extra": {
                    "stars": r.get("stargazers_count", 0),
                    "language": r.get("language") or "",
                    "topics": (r.get("topics") or [])[:6],
                    "created": (r.get("created_at") or "")[:10],
                    "pushed": (r.get("pushed_at") or "")[:10],
                },
            }
        if i < len(queries) - 1:
            time.sleep(1 if token else 7)  # 未认证搜索限速 10 次/分钟
    log(f"[github] 命中 {len(seen_repos)} 个仓库")
    return list(seen_repos.values())


def collect_youtube(cfg, now):
    if not cfg.get("enabled"):
        return []
    log("[youtube] 拉取频道 RSS …")
    max_age = int(cfg.get("max_age_days", 14))
    per_channel = int(cfg.get("per_channel", 4))

    def one(ch):
        cid = ch.get("id")
        if not cid:
            return []
        raw = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        items = []
        for it in parse_feed(raw, default_link=f"https://www.youtube.com/channel/{cid}")[: per_channel * 3]:
            if not it["dt"] or (now - it["dt"]).days > max_age:
                continue
            vid = ""
            m = re.search(r"[?&]v=([\w-]{11})", it["link"] or "")
            if m:
                vid = m.group(1)
            items.append(
                {
                    "source": "youtube",
                    "source_label": f"YouTube · {ch['name']}",
                    "title": it["title"],
                    "link": it["link"],
                    "dt": it["dt"],
                    "summary": it["summary"],
                    "extra": {"channel": ch["name"], "video_id": vid},
                }
            )
            if len(items) >= per_channel:
                break
        return items

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(one, cfg.get("channels", [])):
            out.extend(res)
    log(f"[youtube] 命中 {len(out)} 个视频")
    return out


def collect_feeds(cfg, now):
    if not cfg.get("enabled"):
        return []
    log("[feeds] 拉取博客 RSS …")
    max_age = int(cfg.get("max_age_days", 14))
    per_feed = int(cfg.get("per_feed", 5))

    def one(f):
        raw = http_get(f["url"])
        # 允许单个源覆盖全局时效（低频但高价值的博客，如 Lilian Weng）
        max_age = int(f.get("max_age_days", cfg.get("max_age_days", 14)))
        items = []
        for it in parse_feed(raw, default_link=f["url"])[: per_feed * 3]:
            if it["dt"] and (now - it["dt"]).days > max_age:
                continue
            item = {
                "source": "blog",
                "source_label": f["name"],
                "title": it["title"],
                "link": it["link"],
                "dt": it["dt"],
                "summary": it["summary"],
                "extra": {},
            }
            if f.get("weight"):
                item["weight_override"] = float(f["weight"])
            items.append(item)
            if len(items) >= per_feed:
                break
        return items

    out = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(one, cfg.get("list", [])):
            out.extend(res)
    log(f"[feeds] 命中 {len(out)} 篇")
    return out


def collect_scrape(cfg, now):
    """抓取没有 RSS 的官方页面（如 Anthropic news / engineering）。"""
    if not cfg.get("enabled"):
        return []
    log("[scrape] 抓取无 RSS 的官方页面 …")
    out = []
    for page in cfg.get("pages", []):
        raw = http_get(page["url"])
        if not raw:
            continue
        text = raw.decode("utf-8", "replace")
        max_age = int(page.get("max_age_days", 30))
        max_items = int(page.get("max_items", 8))
        weight = page.get("weight")
        parsed = urllib.parse.urlparse(page["url"])
        base = f"{parsed.scheme}://{parsed.netloc}"
        prefix = parsed.path.rstrip("/")
        pat = re.compile(
            r'href="(' + re.escape(prefix) + r'/[a-z0-9][a-z0-9\-]{3,})"'
        )
        seen, n = set(), 0
        for m in pat.finditer(text):
            path = m.group(1)
            if path in seen:
                continue
            seen.add(path)
            # 从周边 HTML 里抠标题和日期
            chunk = text[m.start() : m.start() + 3000]
            t = re.search(r"<h[23][^>]*>(.*?)</h[23]>", chunk, re.S)
            title = strip_tags(t.group(1)) if t else ""
            if not title or len(title) < 8:
                title = path.rsplit("/", 1)[-1].replace("-", " ").title()
            dm = re.search(r'<time[^>]*dateTime="([^"]+)"', chunk)
            d = dm.group(1) if dm else None
            if not d:
                dt_m = re.search(r"(\w{3,9}\s+\d{1,2},\s+\d{4})", chunk)
                d = dt_m.group(1) if dt_m else None
            dt = parse_dt(d) if d else None
            if dt and (now - dt).days > max_age:
                continue
            item = {
                "source": "blog",
                "source_label": page["name"],
                "title": title,
                "link": base + path,
                "dt": dt,
                "summary": "",
                "extra": {},
            }
            if weight:
                item["weight_override"] = float(weight)
            out.append(item)
            n += 1
            if n >= max_items:
                break
        log(f"[scrape] {page['name']} 命中 {n} 篇")
    return out


def collect_papers(cfg, now):
    if not cfg.get("enabled"):
        return []
    log("[papers] 拉取论文 …")
    out = []

    if cfg.get("hf_daily_papers"):
        raw = http_get("https://huggingface.co/api/daily_papers")
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                log(f"  [warn] HF daily_papers JSON 失败: {e}")
                data = []
            for p in data[: int(cfg.get("max_items", 12))]:
                paper = p.get("paper") or {}
                title = paper.get("title") or p.get("title")
                pid = paper.get("id") or p.get("id")
                if not title or not pid:
                    continue
                out.append(
                    {
                        "source": "paper",
                        "source_label": "HF Daily Papers",
                        "title": title,
                        "link": f"https://huggingface.co/papers/{pid}",
                        "dt": parse_dt(p.get("publishedAt") or p.get("date")),
                        "summary": strip_tags(paper.get("summary") or "")[:500],
                        "extra": {
                            "upvotes": paper.get("upvotes", 0),
                            "arxiv": f"https://arxiv.org/abs/{pid}",
                        },
                    }
                )

    per_feed = int(cfg.get("arxiv_per_feed", 5))
    for f in cfg.get("arxiv_rss", []):
        raw = http_get(f["url"])
        n = 0
        for it in parse_feed(raw, default_link="https://arxiv.org")[: per_feed * 2]:
            if it["dt"] and (now - it["dt"]).days > 3:
                continue
            item = {
                "source": "paper",
                "source_label": f["name"],
                "title": it["title"],
                "link": it["link"],
                "dt": it["dt"],
                "summary": it["summary"][:500],
                "extra": {},
            }
            if f.get("weight"):
                item["weight_override"] = float(f["weight"])
            out.append(item)
            n += 1
            if n >= per_feed:
                break
    log(f"[papers] 命中 {len(out)} 篇")
    return out


def collect_hn(cfg, now):
    if not cfg.get("enabled"):
        return []
    log("[hackernews] 搜索高分讨论 …")
    min_points = int(cfg.get("min_points", 80))
    max_age = int(cfg.get("max_age_days", 2))
    cutoff = int((now - timedelta(days=max_age)).timestamp())
    out, seen = [], set()
    for q in cfg.get("queries", []):
        url = (
            "https://hn.algolia.com/api/v1/search_by_date?tags=story&query="
            + urllib.parse.quote(q)
            + f"&numericFilters=points%3E{min_points},created_at_i%3E{cutoff}&hitsPerPage=20"
        )
        raw = http_get(url)
        if not raw:
            log(f"  [warn] HN query 无响应: {q}")
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"  [warn] HN query 解析失败 {q}: {e} | {raw[:200]!r}")
            continue
        hits = data.get("hits") or []
        log(f"  [hn] {q}: {len(hits)} 条 (nbHits={data.get('nbHits')})")
        if not hits:
            # 空结果时把响应体打出来，方便定位是限流还是真的没数据
            log(f"       body: {raw[:220]!r}")
        for h in hits:
            title = h.get("title")
            oid = h.get("objectID")
            if not title or not oid or oid in seen:
                continue
            seen.add(oid)
            out.append(
                {
                    "source": "hackernews",
                    "source_label": "Hacker News",
                    "title": title,
                    "link": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "dt": parse_dt(h.get("created_at")),
                    "summary": strip_tags(h.get("story_text") or "")[:400],
                    "extra": {
                        "points": h.get("points", 0),
                        "comments": h.get("num_comments", 0),
                        "hn_link": f"https://news.ycombinator.com/item?id={oid}",
                    },
                }
            )
    out.sort(key=lambda x: x["extra"].get("points", 0), reverse=True)
    out = out[: int(cfg.get("max_items", 10))]
    log(f"[hackernews] 命中 {len(out)} 条")
    return out


# --------------------------------------------------------------------------
# 过滤 / 去重 / 相似 / 打分
# --------------------------------------------------------------------------

def is_relevant(item, keywords):
    if item["source"] == "github":
        return True  # 已按 topic 搜过
    hay = (item["title"] + " " + item["summary"]).lower()
    return any(k.lower() in hay for k in keywords)


def tokenize(title):
    words = re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", title.lower())
    stop = {
        "the", "a", "an", "for", "and", "with", "from", "your", "you", "how", "what",
        "new", "using", "into", "this", "that", "are", "can", "why", "when", "its",
        "via", "all", "out", "not", "but", "has", "have", "will", "more", "most",
        "i", "to", "of", "in", "on", "is", "it", "at", "by", "or", "as", "be",
    }
    return {w for w in words if w not in stop}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score_item(item, scoring, now):
    w = item.get("weight_override") or scoring["source_weight"].get(item["source"], 0.7)
    days = (now - item["dt"]).days if item["dt"] else 99
    rf = 0.5
    for k in sorted(scoring["recency_factor"], key=int):
        if days <= int(k):
            rf = scoring["recency_factor"][k]
            break
    boost = 1.0
    hay = (item["title"] + " " + item["summary"]).lower()
    if any(k.lower() in hay for k in scoring["hot_keywords"]):
        boost *= float(scoring["hot_boost"])
    # 面试硬通货：系统设计 / 从零实现 / 深入原理。只对博客和视频生效，
    # 论文不加这个加成（否则 arXiv 会被顶得过高）。
    if item["source"] in ("blog", "youtube") and any(
        k.lower() in hay for k in scoring.get("interview_keywords", [])
    ):
        boost *= float(scoring.get("interview_boost", 1.0))
    base = 1.0
    ex = item.get("extra", {})
    if item["source"] == "github":
        base = 1.0 + min(3.0, __import__("math").log10(max(10, ex.get("stars", 0))))
    elif item["source"] == "hackernews":
        base = 1.0 + min(3.0, __import__("math").log10(max(10, ex.get("points", 0))))
    elif item["source"] == "paper":
        base = 1.0 + min(2.0, __import__("math").log10(max(10, ex.get("upvotes", 10))))
    return round(w * rf * boost * base, 3)


# --------------------------------------------------------------------------
# DeepSeek 中文摘要
# --------------------------------------------------------------------------

SUMMARY_SYSTEM = (
    "你是资深 AI 技术情报编辑，服务对象是 AI 工程师。"
    "你只输出 JSON，不输出任何其他文字、markdown 代码块或解释。"
)


def llm_providers():
    """按优先级返回可用的 LLM 通道（都兼容 OpenAI 协议）。"""
    out = []
    nv = os.environ.get("NVIDIA_API_KEY")
    if nv:
        out.append(
            {
                "name": "nvidia",
                "base": os.environ.get(
                    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
                ).rstrip("/"),
                "model": os.environ.get(
                    "NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b"
                ),
                "key": nv,
            }
        )
    ds = os.environ.get("DEEPSEEK_API_KEY")
    if ds:
        out.append(
            {
                "name": "deepseek",
                "base": os.environ.get(
                    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
                ).rstrip("/"),
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                "key": ds,
            }
        )
    return out


def build_prompt(batch):
    lines = []
    for i, it in enumerate(batch):
        meta = ""
        ex = it.get("extra", {})
        if it["source"] == "github":
            meta = f" (⭐{ex.get('stars', 0)}, {ex.get('language') or 'n/a'})"
        elif it["source"] == "hackernews":
            meta = f" (HN {ex.get('points', 0)} 分)"
        elif it["source"] == "paper":
            meta = " (论文)"
        lines.append(
            f"[{i}] 来源：{it['source_label']}{meta}\n"
            f"    标题：{it['title']}\n"
            f"    原文简介：{(it['summary'] or '(无)')[:400]}"
        )
    return (
        "下面是今天采集到的 AI 前沿动态条目。请为每一条输出：\n"
        '  "summary": 一句中文摘要（不超过 70 字，说清楚它做了什么 / 发了什么，'
        "保留专有名词原文，不要客套话）\n"
        '  "why": 一句中文「为什么值得关注」（不超过 60 字，面向 AI 工程师）\n'
        '严格输出 JSON：{"items":[{"id":0,"summary":"…","why":"…"}, …]}，'
        "id 必须与输入编号一致，不要遗漏任何编号。\n\n" + "\n\n".join(lines)
    )


def call_llm(p, user):
    """调一个通道，返回 {id: {...}}；失败返回 None。"""
    resp = http_post_json(
        f"{p['base']}/chat/completions",
        {
            "model": p["model"],
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {p['key']}"},
    )
    if not resp:
        return None
    try:
        content = resp["choices"][0]["message"].get("content")
        if not content:
            log(f"  [warn] {p['name']} 返回空内容（可能被推理占满 token）")
            return None
        data = json.loads(content)
        arr = data.get("items") or data.get("data") or []
        return {int(x["id"]): x for x in arr if "id" in x}
    except Exception as e:  # noqa: BLE001
        log(f"  [warn] {p['name']} 摘要解析失败: {e}")
        return None


def summarize(items, now):
    providers = llm_providers()
    if not providers:
        log("[llm] 未设置 NVIDIA_API_KEY / DEEPSEEK_API_KEY，跳过中文摘要")
        return
    if os.environ.get("DRY_RUN") == "1":
        log("[llm] DRY_RUN=1，跳过摘要")
        return
    todo = [it for it in items if not it.get("summary_zh")]
    log(
        "[llm] 通道 "
        + " -> ".join(f"{p['name']}({p['model']})" for p in providers)
        + f"，待摘要 {len(todo)} 条"
    )

    batch_size = 6
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        user = build_prompt(batch)
        by_id = None
        used = ""
        for p in providers:
            by_id = call_llm(p, user)
            if by_id:
                used = p["name"]
                break
        if by_id is None:
            log("  [warn] 本批所有通道均失败，保留原文简介")
            continue
        for i, it in enumerate(batch):
            got = by_id.get(i)
            if got:
                it["summary_zh"] = (got.get("summary") or "").strip()
                it["why_zh"] = (got.get("why") or "").strip()
        log(f"  [llm] 第 {start // batch_size + 1} 批完成（{used}）")
        if start + batch_size < len(todo):
            time.sleep(1)


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"items": {}}


def save_state(state, now, keep_days):
    cutoff = now - timedelta(days=keep_days)
    keep = {}
    for url, rec in state.get("items", {}).items():
        dt = parse_dt(rec.get("date", ""))
        if dt is None or dt >= cutoff:
            keep[url] = rec
    state["items"] = keep
    state["updated"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def annotate(items, state, threshold):
    """标注 new / dup / similar，并做批内去重。"""
    known = state.get("items", {})
    known_tokens = [
        (u, set(r.get("tokens") or []), r.get("title", ""), r.get("date", ""))
        for u, r in known.items()
    ]
    kept, seen_today = [], []
    for it in sorted(items, key=lambda x: x.get("score", 0), reverse=True):
        url = it["link"]
        if url in known:
            it["flag"] = "dup"
            it["dup_note"] = f"已于 {known[url].get('date', '?')} 推荐过"
            # 复用上次的中文摘要，避免重复提醒段落没摘要
            prev = known[url]
            if prev.get("summary_zh"):
                it["summary_zh"] = prev["summary_zh"]
            if prev.get("why_zh"):
                it["why_zh"] = prev["why_zh"]
            kept.append(it)
            continue
        toks = tokenize(it["title"])
        it["tokens"] = sorted(toks)
        best, note = 0.0, ""
        for u, kt, ktitle, kdate in known_tokens:
            if u == url:
                continue
            r = jaccard(toks, kt)
            if r > best:
                best, note = r, f"与 {kdate} 的「{ktitle}」相似度 {int(r * 100)}%"
        for jt, jtitle in seen_today:
            r = jaccard(toks, jt)
            if r > best:
                best, note = r, f"与今天已收录的「{jtitle}」相似度 {int(r * 100)}%"
        if best >= threshold:
            it["flag"] = "similar"
            it["dup_note"] = note
        else:
            it["flag"] = "new"
        seen_today.append((toks, it["title"]))
        kept.append(it)
    return kept


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def fmt_item_line(it, idx=None):
    ex = it.get("extra", {})
    badge = ""
    if it["source"] == "github":
        badge = f"⭐ {ex.get('stars', 0)}"
        if ex.get("language"):
            badge += f" · {ex['language']}"
        if ex.get("created"):
            badge += f" · 新建 {ex['created']}"
    elif it["source"] == "hackernews":
        badge = f"🔥 HN {ex.get('points', 0)} 分 · 💬 {ex.get('comments', 0)}"
    elif it["source"] == "paper":
        badge = f"📄 HF 👍 {ex.get('upvotes', 0)}" if ex.get("upvotes") else "📄 论文"
    elif it["source"] == "youtube":
        badge = "🎬 视频"
    else:
        badge = "📝 文章"
    date_s = it["dt"].astimezone(CST).strftime("%m-%d") if it["dt"] else "?"

    head = f"### {idx}. [{it['title']}]({it['link']})" if idx else f"- **[{it['title']}]({it['link']})**"
    lines = [head, f"  `{it['source_label']}` · {badge} · {date_s}"]
    if it.get("summary_zh"):
        lines.append(f"  **摘要**：{it['summary_zh']}")
    if it.get("why_zh"):
        lines.append(f"  **为什么值得看**：{it['why_zh']}")
    elif it.get("summary"):
        lines.append(f"  {it['summary'][:220]}")
    if it.get("dup_note"):
        lines.append(f"  🔁 {it['dup_note']}")
    return "\n".join(lines)


def select_items(items, out_cfg):
    """按源配额选条目，避免 GitHub 星数把其他源全挤掉。"""
    max_items = int(out_cfg.get("max_items", 30))
    quotas = out_cfg.get("source_quota") or {}
    by_src = {}
    for it in items:
        by_src.setdefault(it["source"], []).append(it)

    chosen, links = [], set()
    for src, quota in quotas.items():
        for it in (by_src.get(src) or [])[: int(quota)]:
            chosen.append(it)
            links.add(it["link"])
    for it in items:
        if len(chosen) >= max_items:
            break
        if it["link"] not in links:
            chosen.append(it)
            links.add(it["link"])
    chosen.sort(key=lambda x: x["score"], reverse=True)
    return chosen[:max_items]


def pick_top(fresh, n):
    """精选：每个来源先各取最好的一个，再按分数补足，保证来源多样。"""
    best_per_src, chosen, links = {}, [], set()
    for it in fresh:
        best_per_src.setdefault(it["source"], it)
    for s in ["hackernews", "paper", "blog", "youtube", "github"]:
        if s in best_per_src:
            chosen.append(best_per_src[s])
            links.add(best_per_src[s]["link"])
    for it in fresh:
        if len(chosen) >= n:
            break
        if it["link"] not in links:
            chosen.append(it)
            links.add(it["link"])
    chosen.sort(key=lambda x: x["score"], reverse=True)
    return chosen[:n]


def render(date_str, items, dup, now, top_n):
    fresh = [i for i in items if i["flag"] == "new"]
    top = pick_top(fresh, top_n)
    top_links = {i["link"] for i in top}

    by_src = {"github": [], "youtube": [], "blog": [], "paper": [], "hackernews": []}
    for it in fresh:
        if it["link"] in top_links:
            continue
        by_src.setdefault(it["source"], []).append(it)

    out = [
        f"# AI 前沿日报 · {date_str}",
        "",
        f"> 生成时间：{now.astimezone(CST).strftime('%Y-%m-%d %H:%M')} (CST)　|　"
        f"共 {len(items) + len(dup)} 条（🆕 新增 {len(items)} · 🔁 重复/相似 {len(dup)}）",
        ">",
        "> 数据源：GitHub · YouTube · 博客 RSS · HF Daily Papers · arXiv · Hacker News",
        "> 摘要由 NVIDIA NIM / DeepSeek 生成，链接均为原始出处，请以原文为准。",
        "",
    ]

    if FETCH_FAILURES:
        out.append("## ⚠️ 数据源访问异常")
        out.append("")
        out.append("本次运行中以下站点访问失败，对应内容可能缺失：")
        out.append("")
        for host, n in sorted(FETCH_FAILURES.items()):
            out.append(f"- `{host}` — 失败 {n} 次")
        out.append("")

    if SOURCE_COUNT:
        out.append(
            "源命中数："
            + " · ".join(f"{k} {v}" for k, v in SOURCE_COUNT.items())
        )
        out.append("")

    out += [
        "---",
        "",
        f"## 📌 今日精选 Top {len(top)}",
        "",
    ]
    for i, it in enumerate(top, 1):
        out.append(fmt_item_line(it, i))
        out.append("")

    titles = {
        "github": "🐙 GitHub 热门 / 新项目",
        "youtube": "📺 YouTube 新视频",
        "paper": "📄 论文 & 研究",
        "blog": "📝 博客 & 技术文章",
        "hackernews": "💬 Hacker News 热议",
    }
    for key in ["github", "youtube", "paper", "blog", "hackernews"]:
        group = by_src.get(key) or []
        if not group:
            continue
        out.append(f"## {titles[key]}（{len(group)}）")
        out.append("")
        for it in group:
            out.append(fmt_item_line(it))
            out.append("")

    if dup:
        out.append(f"## 🔁 重复 / 相似提醒（{len(dup)}）")
        out.append("")
        out.append("> 以下条目此前推荐过，或与历史条目高度相似，仅列出供你参考。")
        out.append("")
        for it in dup:
            out.append(fmt_item_line(it))
            out.append("")

    out.append("---")
    out.append("")
    out.append("*由 GitHub Actions 每天 16:00 (CST) 自动生成*")
    return "\n".join(out).rstrip() + "\n"


def update_readme(digest_dir, now):
    files = sorted(digest_dir.glob("*.md"), reverse=True)
    lines = [
        "# AI 前沿日报",
        "",
        "每天 16:00 (CST) 自动采集 GitHub / YouTube / 博客 / 论文 / HN 上关于 "
        "AI、LLM、Agent、Harness 的前沿动态，生成中文摘要。",
        "",
        "🌐 **在线阅读：[ai-daily-digest.pages.dev](https://ai-daily-digest.pages.dev)**"
        "（[归档](https://ai-daily-digest.pages.dev/archive.html) · "
        "[RSS](https://ai-daily-digest.pages.dev/feed.xml)）",
        "",
        "## 最新",
        "",
    ]
    if files:
        latest = files[0]
        lines.append(f"👉 **[{latest.stem}](digest/{latest.name})**")
        lines.append("")
        weekly = sorted((ROOT / "weekly").glob("*.md"), reverse=True) if (ROOT / "weekly").exists() else []
        if weekly:
            lines.append(f"📌 本周精选：**[{weekly[0].stem}](weekly/{weekly[0].name})**")
            lines.append("")
        lines.append("## 历史归档")
        lines.append("")
        for f in files:
            lines.append(f"- [{f.stem}](digest/{f.name})")
    else:
        lines.append("_还没有日报，等待第一次运行。_")
    lines.append("")
    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    log(f"=== 采集开始 {now.astimezone(CST).isoformat()} ===")

    items = []
    for fn, c in (
        (collect_github, cfg.get("github", {})),
        (collect_youtube, cfg.get("youtube", {})),
        (collect_feeds, cfg.get("feeds", {})),
        (collect_scrape, cfg.get("scrape", {})),
        (collect_papers, cfg.get("papers", {})),
        (collect_hn, cfg.get("hackernews", {})),
    ):
        try:
            items.extend(fn(c, now))
        except Exception as e:  # noqa: BLE001
            log(f"  [error] {fn.__name__} 失败: {type(e).__name__}: {e}")

    for it in items:
        SOURCE_COUNT[it["source"]] = SOURCE_COUNT.get(it["source"], 0) + 1
    log(f"[stat] 各源命中：{SOURCE_COUNT}")

    # 去掉没有链接的（各采集器已按自己的时效窗口过滤，这里不再重复砍一刀）
    items = [i for i in items if i.get("link")]
    # 相关性过滤
    kws = cfg["relevance"]["keywords"]
    before = len(items)
    items = [i for i in items if is_relevant(i, kws)]
    log(f"[filter] 相关性过滤 {before} -> {len(items)}")

    # 打分
    for it in items:
        it["score"] = score_item(it, cfg["scoring"], now)
    # 去重 + 相似
    out_cfg = cfg["output"]
    all_items = annotate(items, load_state(), float(out_cfg.get("similarity_threshold", 0.6)))
    all_items.sort(key=lambda x: x["score"], reverse=True)

    # 主列表只放「新」内容；重复/相似的单独列出提醒
    fresh = [i for i in all_items if i["flag"] == "new"]
    dups = [i for i in all_items if i["flag"] != "new"]
    dups = dups[: int(out_cfg.get("max_dups", 10))]
    items = select_items(fresh, out_cfg)

    # 摘要（只对入选的新条目调用 LLM，省 token）
    summarize(items, now)

    date_str = now.astimezone(CST).strftime("%Y-%m-%d")
    top_n = int(out_cfg.get("top_picks", 6))
    md = render(date_str, items, dups, now, top_n)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIGEST_DIR / f"{date_str}.md"
    out_path.write_text(md, encoding="utf-8")
    log(f"[done] 已写出 {out_path}")

    # 同一份内容同时存一份结构化 JSON，供静态站生成使用
    def jsonable(it):
        d = dict(it)
        d["dt"] = it["dt"].isoformat() if it.get("dt") else None
        return d

    data_path = DIGEST_DIR / f"{date_str}.json"
    data_path.write_text(
        json.dumps(
            {
                "date": date_str,
                "generated": now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                "source_count": SOURCE_COUNT,
                "failures": FETCH_FAILURES,
                "items": [jsonable(i) for i in items],
                "dups": [jsonable(i) for i in dups],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    log(f"[done] 已写出 {data_path}")

    # 更新状态（新条目 + 提醒过的重复条目都记下，避免反复提醒；
    # 同时存中文摘要，下次重复推荐时直接复用，不再多花 token）
    state = load_state()
    for it in items + dups:
        state["items"][it["link"]] = {
            "title": it["title"],
            "date": date_str,
            "tokens": it.get("tokens", []),
            "source": it["source"],
            "summary_zh": it.get("summary_zh", ""),
            "why_zh": it.get("why_zh", ""),
        }
    save_state(state, now, int(out_cfg.get("keep_state_days", 120)))
    update_readme(DIGEST_DIR, now)
    log(f"=== 完成：{len(items)} 条 ===")


if __name__ == "__main__":
    main()
