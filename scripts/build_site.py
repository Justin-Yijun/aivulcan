#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 前沿日报 · 静态站生成器
---------------------------
读取 digest/*.json，生成可直接托管的静态网站到 site/：

  site/index.html            最新一期
  site/d/YYYY-MM-DD.html     每期独立页面（永久链接）
  site/archive.html          历史归档（按月份组）
  site/about.html            关于
  site/feed.xml              RSS 订阅
  site/style.css             样式

仅使用 Python 标准库。由 Cloudflare Pages 直接托管 site/ 目录。
"""

import html
import json
import os
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = ROOT / "digest"
WEEKLY_DIR = ROOT / "weekly"
SITE_DIR = ROOT / "site"

CST = timezone(timedelta(hours=8))
SITE_TITLE = "AI 前沿日报"
SITE_DESC = (
    "每天自动采集 GitHub / YouTube / 博客 / 论文 / Hacker News 上"
    "关于 AI、LLM、Agent、Harness 的前沿动态，生成中文摘要。"
)
# Cloudflare Pages 项目建好后，把这里换成实际地址（或设环境变量 SITE_URL）
SITE_URL = os.environ.get("SITE_URL", "https://ai-daily-digest.pages.dev").rstrip("/")

SRC_NAME = {
    "github": "GitHub",
    "youtube": "YouTube",
    "blog": "博客",
    "paper": "论文",
    "hackernews": "Hacker News",
}
SRC_ICON = {
    "github": "🐙",
    "youtube": "📺",
    "blog": "📝",
    "paper": "📄",
    "hackernews": "💬",
}

STYLE = """\
:root{--bg:#fbfbfd;--fg:#1c1c1e;--muted:#6b6b70;--card:#fff;--line:#e6e6ea;--link:#0a5cd6;--tag:#f0f0f4}
@media (prefers-color-scheme:dark){:root{--bg:#131316;--fg:#e8e8ea;--muted:#9a9aa0;--card:#1c1c20;--line:#2c2c32;--link:#6ea8fe;--tag:#26262c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:28px 18px 80px}
header.site{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:26px}
header.site h1{margin:0 0 6px;font-size:1.5rem}
header.site p{margin:0;color:var(--muted);font-size:.9rem}
header.site nav{margin-top:12px;font-size:.9rem}
header.site nav a{margin-right:14px}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:1.6rem;line-height:1.35}
h2{font-size:1.15rem;margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:1.02rem;margin:0 0 6px;line-height:1.5}
.meta{color:var(--muted);font-size:.86rem;margin:0 0 8px}
.sum{margin:0 0 4px;font-size:.94rem}
.why{margin:0 0 4px;font-size:.94rem;color:var(--muted)}
.dup{color:var(--muted);font-size:.86rem;margin:6px 0 0}
.pick,.item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}
.pick{border-left:3px solid var(--link)}
.tag{display:inline-block;background:var(--tag);border-radius:6px;padding:1px 7px;font-size:.78rem;color:var(--muted);margin-right:6px}
.date{color:var(--muted);font-size:.8rem;margin-left:2px}
blockquote{margin:0 0 22px;padding:10px 14px;border-left:3px solid var(--line);background:var(--card);border-radius:0 8px 8px 0;color:var(--muted);font-size:.9rem}
blockquote p{margin:0}
.warn{background:var(--card);border:1px solid #e0b34a;border-left:3px solid #e0b34a;border-radius:10px;padding:12px 16px;margin-bottom:22px;font-size:.9rem}
.warn ul{margin:6px 0 0;padding-left:20px}
footer{margin-top:50px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}
.months h3{color:var(--muted);font-weight:600;font-size:.95rem;margin:22px 0 8px}
.archive-list{list-style:none;padding:0;margin:0}
.archive-list li{padding:7px 0;border-bottom:1px solid var(--line);font-size:.95rem}
.archive-list .cnt{color:var(--muted);font-size:.85rem;margin-left:8px}
code{background:var(--tag);border-radius:5px;padding:1px 5px;font-size:.86em}
.pager{display:flex;justify-content:space-between;font-size:.9rem;margin:26px 0}
@media(max-width:560px){.wrap{padding:20px 14px 60px}h1{font-size:1.35rem}.pick,.item{padding:12px 13px}}
"""


def esc(s):
    return html.escape(s or "", quote=True)


def parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_all():
    out = []
    for p in sorted(DIGEST_DIR.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读取失败 {p.name}: {e}")
    return out


def load_weeklies():
    out = []
    if not WEEKLY_DIR.exists():
        return out
    for p in sorted(WEEKLY_DIR.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读取失败 {p.name}: {e}")
    return out


def week_of(date_s):
    """由日期字符串推出 ISO 周号，用于互链。"""
    dt = parse_dt(date_s)
    if not dt:
        return ""
    y, w, _ = dt.astimezone(CST).isocalendar()
    return f"{y}-W{w:02d}"


def badges(it):
    ex = it.get("extra") or {}
    src = it.get("source")
    b = []
    if src == "github":
        b.append(f"⭐ {ex.get('stars', 0)}")
        if ex.get("language"):
            b.append(esc(ex["language"]))
        if ex.get("created"):
            b.append(f"新建 {esc(ex['created'])}")
    elif src == "hackernews":
        b.append(f"🔥 {ex.get('points', 0)} 分")
        b.append(f"💬 {ex.get('comments', 0)}")
    elif src == "paper":
        b.append(f"👍 {ex['upvotes']}" if ex.get("upvotes") else "论文")
    elif src == "youtube":
        b.append("视频")
    else:
        b.append("文章")
    return b


def item_html(it, idx=None, cls="item"):
    dt = parse_dt(it.get("dt"))
    date_s = dt.astimezone(CST).strftime("%m-%d") if dt else ""
    tags = "".join(f'<span class="tag">{t}</span>' for t in badges(it))
    head = (
        f'<h3>{idx}. <a href="{esc(it.get("link"))}" target="_blank" '
        f'rel="noopener">{esc(it.get("title"))}</a></h3>'
        if idx
        else f'<h3><a href="{esc(it.get("link"))}" target="_blank" '
        f'rel="noopener">{esc(it.get("title"))}</a></h3>'
    )
    parts = [
        f'<article class="{cls}">',
        head,
        f'<p class="meta"><span class="tag">{esc(it.get("source_label"))}</span>'
        f'{tags}<span class="date">{date_s}</span></p>',
    ]
    if it.get("summary_zh"):
        parts.append(f'<p class="sum">{esc(it["summary_zh"])}</p>')
    elif it.get("summary"):
        parts.append(f'<p class="sum">{esc(it["summary"][:260])}</p>')
    if it.get("why_zh"):
        parts.append(f'<p class="why">为什么值得看：{esc(it["why_zh"])}</p>')
    if it.get("dup_note"):
        parts.append(f'<p class="dup">🔁 {esc(it["dup_note"])}</p>')
    parts.append("</article>")
    return "\n".join(parts)


def page(title, body, desc="", canonical="", rel_root="", extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
{extra_head}<link rel="stylesheet" href="{rel_root}style.css">
<link rel="alternate" type="application/rss+xml" title="{esc(SITE_TITLE)}" href="{SITE_URL}/feed.xml">
</head>
<body>
<div class="wrap">
<header class="site">
<h1><a href="{rel_root}index.html">{esc(SITE_TITLE)}</a></h1>
<p>{esc(SITE_DESC)}</p>
<nav><a href="{rel_root}index.html">最新</a><a href="{rel_root}archive.html">归档</a><a href="{rel_root}feed.xml">RSS</a><a href="{rel_root}about.html">关于</a></nav>
</header>
{body}
<footer>
<p>{esc(SITE_TITLE)} · 由 GitHub Actions 每天 16:00 (CST) 自动生成 · 内容版权归原作者所有，链接均为原始出处</p>
</footer>
</div>
</body>
</html>
"""


def render_digest(d, rel_root="", canonical="", week=""):
    items = d.get("items") or []
    dups = d.get("dups") or []
    cnt = d.get("source_count") or {}
    fails = d.get("failures") or {}

    body = [f'<h1>{esc(d.get("date"))} 日报</h1>']
    meta = f'生成时间 {esc(d.get("generated"))} (CST) ｜ 共 {len(items) + len(dups)} 条'
    if dups:
        meta += f'（新增 {len(items)} · 重复/相似 {len(dups)}）'
    if week:
        meta += f'　｜　<a href="{rel_root}w/{esc(week)}.html">本周精选 →</a>'
    body.append(f'<p class="meta">{meta}</p>')
    if cnt:
        body.append(
            '<p class="meta">源命中数：'
            + " · ".join(f"{esc(SRC_NAME.get(k, k))} {v}" for k, v in cnt.items())
            + "</p>"
        )

    if fails:
        lis = "".join(f"<li><code>{esc(h)}</code> — 失败 {n} 次</li>" for h, n in sorted(fails.items()))
        body.append(
            '<div class="warn"><strong>⚠️ 数据源访问异常</strong>'
            "<p>本次运行中以下站点访问失败，对应内容可能缺失：</p>"
            f"<ul>{lis}</ul></div>"
        )

    top_n = min(6, len(items))
    if top_n:
        body.append(f"<h2>📌 今日精选 Top {top_n}</h2>")
        for i, it in enumerate(items[:top_n], 1):
            body.append(item_html(it, idx=i, cls="pick"))

    for key in ("github", "youtube", "paper", "blog", "hackernews"):
        group = [i for i in items[top_n:] if i.get("source") == key]
        if not group:
            continue
        body.append(f"<h2>{SRC_ICON[key]} {SRC_NAME[key]}（{len(group)}）</h2>")
        for it in group:
            body.append(item_html(it))

    if dups:
        body.append(f"<h2>🔁 重复 / 相似提醒（{len(dups)}）</h2>")
        body.append(
            "<blockquote><p>以下条目此前推荐过，或与历史条目高度相似，仅列出供参考。</p></blockquote>"
        )
        for it in dups:
            body.append(item_html(it, cls="item"))

    if canonical:
        body.append(f'<p class="meta" style="margin-top:26px">永久链接：<a href="{esc(canonical)}">{esc(canonical)}</a></p>')

    return page(
        f'{d.get("date")} · {SITE_TITLE}',
        "\n".join(body),
        desc=f'{d.get("date")} AI / LLM / Agent 前沿日报',
        canonical=canonical,
        rel_root=rel_root,
    )


def render_weekly(w, rel_root=""):
    items = w.get("items") or []
    body = [f'<h1>本周精选 · {esc(w.get("week"))}</h1>']
    body.append(
        f'<p class="meta">覆盖 {esc(w.get("start"))} ~ {esc(w.get("end"))}'
        f'　｜　共 {len(items)} 条　｜　生成 {esc(w.get("generated"))} (CST)</p>'
    )
    for i, it in enumerate(items, 1):
        body.append(item_html(it, idx=i, cls="pick"))
    return page(
        f'本周精选 {w.get("week")} · ' + SITE_TITLE,
        "\n".join(body),
        desc=f'{w.get("start")} ~ {w.get("end")} AI / LLM / Agent 本周精选',
        rel_root=rel_root,
    )


def render_archive(all_d, all_w):
    body = ["<h1>历史归档</h1>"]
    if all_w:
        body.append('<div class="months"><h3>📌 周报</h3><ul class="archive-list">')
        for w in all_w:
            n = len(w.get("items") or [])
            top = (w.get("items") or [{}])[0].get("title") if w.get("items") else ""
            body.append(
                f'<li><a href="w/{esc(w.get("week"))}.html">'
                f'{esc(w.get("start"))} ~ {esc(w.get("end"))}</a>'
                f'<span class="cnt">{n} 条 · {esc((top or "")[:46])}</span></li>'
            )
        body.append("</ul></div>")
    months = {}
    for d in all_d:
        months.setdefault((d.get("date") or "")[:7], []).append(d)
    for m in sorted(months, reverse=True):
        body.append(f'<div class="months"><h3>{esc(m)}</h3><ul class="archive-list">')
        for d in months[m]:
            n = len(d.get("items") or [])
            top = (d.get("items") or [{}])[0].get("title") if d.get("items") else ""
            body.append(
                f'<li><a href="d/{esc(d.get("date"))}.html">{esc(d.get("date"))}</a>'
                f'<span class="cnt">{n} 条 · {esc((top or "")[:46])}</span></li>'
            )
        body.append("</ul></div>")
    return page("历史归档 · " + SITE_TITLE, "\n".join(body), desc="历史归档")


def render_about(all_d, all_w):
    body = [
        "<h1>关于</h1>",
        f"<p>{esc(SITE_DESC)}</p>",
        "<h2>数据源</h2>",
        "<ul>"
        "<li><strong>GitHub</strong>：最近 45 天新建、按星数排序的 llm / ai-agents / mcp / rag 等 topic 仓库</li>"
        "<li><strong>YouTube</strong>：OpenAI、Anthropic、DeepMind、LangChain、Karpathy 等 16 个频道的更新</li>"
        "<li><strong>博客</strong>：OpenAI / DeepMind / HuggingFace / LangChain 官方、Simon Willison、"
        "Lilian Weng、The Gradient、Interconnects、Latent Space、新智元、量子位等 22 个源</li>"
        "<li><strong>论文</strong>：HuggingFace Daily Papers、arXiv cs.AI / cs.CL</li>"
        "<li><strong>官方页</strong>：Anthropic news / engineering（无 RSS，直接抓页面）</li>"
        "<li><strong>社区</strong>：Hacker News 高分讨论</li>"
        "</ul>",
        "<h2>怎么做的</h2>",
        "<p>每天 16:00（北京时间）由 GitHub Actions 自动采集，"
        "按来源配额与关键词打分排序，用大模型生成中文摘要，"
        "并与历史记录比对去重、标记相似内容。</p>",
        "<h2>订阅</h2>",
        f'<p>RSS：<a href="{SITE_URL}/feed.xml">{SITE_URL}/feed.xml</a></p>',
        f'<p>全部内容归档在 <a href="https://github.com/Justin-Yijun/ai-daily-digest" '
        f'target="_blank" rel="noopener">GitHub 仓库</a>。</p>',
    ]
    if all_d:
        body.append(f"<p>已累计 {len(all_d)} 期日报" + (f"、{len(all_w)} 期周报" if all_w else "") + "。</p>")
    return page("关于 · " + SITE_TITLE, "\n".join(body), desc="关于 " + SITE_TITLE)


def render_feed(all_d):
    now = datetime.now(timezone.utc)
    items = []
    for d in all_d[:40]:
        date_s = d.get("date") or ""
        dt = parse_dt(date_s)
        picks = (d.get("items") or [])[:6]
        desc = "".join(
            f'<li><a href="{esc(p.get("link"))}">{esc(p.get("title"))}</a>'
            + (f" — {esc(p.get('summary_zh'))}" if p.get("summary_zh") else "")
            + "</li>"
            for p in picks
        )
        items.append(
            "<item>"
            f"<title>{esc(date_s)} AI 前沿日报</title>"
            f"<link>{SITE_URL}/d/{esc(date_s)}.html</link>"
            f"<guid isPermaLink=\"true\">{SITE_URL}/d/{esc(date_s)}.html</guid>"
            + (f"<pubDate>{format_datetime(dt)}</pubDate>" if dt else "")
            + f"<description><![CDATA[<p>{esc(d.get('date'))} 精选：</p><ul>{desc}</ul>"
            f'<p><a href="{SITE_URL}/d/{esc(date_s)}.html">阅读完整日报</a></p>]]></description>'
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
        f"<title>{esc(SITE_TITLE)}</title>\n"
        f"<link>{SITE_URL}/</link>\n"
        f"<description>{esc(SITE_DESC)}</description>\n"
        "<language>zh-cn</language>\n"
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>\n"
        f'<atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )
    return xml


def main():
    all_d = load_all()
    all_w = load_weeklies()
    have_w = {w.get("week") for w in all_w}
    print(f"[site] 读到 {len(all_d)} 期日报、{len(all_w)} 期周报")
    if not all_d:
        print("[site] 没有 digest/*.json，先跑 collect.py")
        return

    (SITE_DIR / "d").mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "style.css").write_text(STYLE, encoding="utf-8")

    for i, d in enumerate(all_d):
        date_s = d.get("date") or f"unknown-{i}"
        canonical = f"{SITE_URL}/d/{date_s}.html"
        wk = week_of(date_s)
        if wk not in have_w:
            wk = ""  # 没有对应的周报页就不放链接，避免死链
        (SITE_DIR / "d" / f"{date_s}.html").write_text(
            render_digest(d, rel_root="../", canonical=canonical, week=wk),
            encoding="utf-8",
        )

    latest_wk = week_of(all_d[0].get("date") or "")
    if latest_wk not in have_w:
        latest_wk = ""
    (SITE_DIR / "index.html").write_text(
        render_digest(all_d[0], rel_root="", week=latest_wk),
        encoding="utf-8",
    )

    if all_w:
        (SITE_DIR / "w").mkdir(parents=True, exist_ok=True)
        for w in all_w:
            (SITE_DIR / "w" / f"{w.get('week')}.html").write_text(
                render_weekly(w, rel_root="../"), encoding="utf-8"
            )

    (SITE_DIR / "archive.html").write_text(render_archive(all_d, all_w), encoding="utf-8")
    (SITE_DIR / "about.html").write_text(render_about(all_d, all_w), encoding="utf-8")
    (SITE_DIR / "feed.xml").write_text(render_feed(all_d), encoding="utf-8")
    (SITE_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8"
    )
    urls = [f"{SITE_URL}/", f"{SITE_URL}/archive.html", f"{SITE_URL}/about.html"]
    urls += [f"{SITE_URL}/d/{d.get('date')}.html" for d in all_d]
    urls += [f"{SITE_URL}/w/{w.get('week')}.html" for w in all_w]
    (SITE_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(f"<url><loc>{u}</loc></url>" for u in urls)
        + "\n</urlset>\n",
        encoding="utf-8",
    )
    print(
        f"[site] 已生成 {SITE_DIR}（首页 + {len(all_d)} 期日报"
        + (f" + {len(all_w)} 期周报" if all_w else "")
        + " + 归档 + RSS + sitemap）"
    )


if __name__ == "__main__":
    main()
