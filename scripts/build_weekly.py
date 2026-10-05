#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 前沿日报 · 周报生成器
-------------------------
把 digest/*.json 里同一 ISO 周（周一~周日）的条目汇总、按链接去重、
按分数排序，取前 N 条生成「本周精选」：

  weekly/YYYY-Www.md     周报正文（Markdown，归档用）
  weekly/YYYY-Www.json   周报结构化数据（站点生成用）

生成时机：周日（CST）生成本周；同时补上周（若缺失）。
条目已带每日生成好的中文摘要，所以周报不再调用大模型，零额外成本。
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = ROOT / "digest"
WEEKLY_DIR = ROOT / "weekly"
CST = timezone(timedelta(hours=8))

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


def log(*a):
    print(*a, flush=True)


def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_days(anchor):
    """返回 anchor 所在 ISO 周的周一~周日 7 个日期字符串。"""
    mon = anchor - timedelta(days=anchor.isoweekday() - 1)
    return [(mon + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def load_digests():
    out = {}
    for p in DIGEST_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out[d.get("date")] = d
        except Exception as e:  # noqa: BLE001
            log(f"  [warn] 读取失败 {p.name}: {e}")
    return out


def pick_week(all_d, days, top_n):
    """汇总这一周的条目：按链接去重（保留最高分），按分数排序取前 N。"""
    best = {}
    for date_s in days:
        d = all_d.get(date_s)
        if not d:
            continue
        for it in d.get("items") or []:
            u = it.get("link")
            if not u:
                continue
            if u not in best or it.get("score", 0) > best[u].get("score", 0):
                best[u] = it
    return sorted(best.values(), key=lambda x: x.get("score", 0), reverse=True)[:top_n]


def fmt_item(it, idx=None):
    ex = it.get("extra") or {}
    src = it.get("source")
    badge = ""
    if src == "github":
        badge = f"⭐ {ex.get('stars', 0)}"
        if ex.get("language"):
            badge += f" · {ex['language']}"
        if ex.get("created"):
            badge += f" · 新建 {ex['created']}"
    elif src == "hackernews":
        badge = f"🔥 HN {ex.get('points', 0)} 分 · 💬 {ex.get('comments', 0)}"
    elif src == "paper":
        badge = f"📄 HF 👍 {ex['upvotes']}" if ex.get("upvotes") else "📄 论文"
    elif src == "youtube":
        badge = "🎬 视频"
    else:
        badge = "📝 文章"
    dt = it.get("dt")
    date_s = str(dt)[5:10] if dt else ""
    head = (
        f"### {idx}. [{it.get('title')}]({it.get('link')})"
        if idx
        else f"- **[{it.get('title')}]({it.get('link')})**"
    )
    lines = [head, f"  `{it.get('source_label')}` · {badge} · {date_s}"]
    if it.get("summary_zh"):
        lines.append(f"  **摘要**：{it['summary_zh']}")
    if it.get("why_zh"):
        lines.append(f"  **为什么值得看**：{it['why_zh']}")
    elif it.get("summary"):
        lines.append(f"  {it['summary'][:200]}")
    return "\n".join(lines)


def render_md(key, days, top, now):
    fresh = [i for i in top if i.get("flag") == "new"]
    n_dup = len(top) - len(fresh)
    out = [
        f"# AI 前沿周报 · {key}",
        "",
        f"> 覆盖 {days[0]} ~ {days[-1]}　|　本周精选 {len(top)} 条"
        + (f"（其中 {n_dup} 条此前推荐过）" if n_dup else ""),
        ">",
        f"> 生成时间：{now.astimezone(CST).strftime('%Y-%m-%d %H:%M')} (CST)",
        ">",
        "> 数据源：GitHub · YouTube · 博客 RSS · HF Daily Papers · arXiv · Hacker News",
        "",
        "---",
        "",
        "## 📌 本周精选",
        "",
    ]
    for i, it in enumerate(top, 1):
        out.append(fmt_item(it, idx=i))
        out.append("")

    by_src = {}
    for it in top:
        by_src.setdefault(it.get("source"), []).append(it)
    for key_s in ("github", "youtube", "paper", "blog", "hackernews"):
        group = by_src.get(key_s) or []
        if not group:
            continue
        out.append(f"## {SRC_ICON[key_s]} {SRC_NAME[key_s]}（{len(group)}）")
        out.append("")
        for it in group:
            out.append(fmt_item(it))
            out.append("")

    out.append("---")
    out.append("")
    out.append("*由 GitHub Actions 自动汇总生成*")
    return "\n".join(out).rstrip() + "\n"


def main():
    now = datetime.now(timezone.utc)
    today = now.astimezone(CST)
    all_d = load_digests()
    if not all_d:
        log("[weekly] 没有 digest/*.json，先跑 collect.py")
        return

    cfg_path = ROOT / "config" / "sources.json"
    top_n = 12
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        top_n = int(cfg.get("output", {}).get("weekly_top", 12))
    except Exception:  # noqa: BLE001
        pass

    # 周日生成本周；任何时候都补一份「上周」（若缺失）
    # 也可用 WEEK_ANCHOR=YYYY-MM-DD 手动补任意一周
    targets = []
    anchor_env = os.environ.get("WEEK_ANCHOR")
    if anchor_env:
        a = None
        try:
            a = datetime.strptime(anchor_env, "%Y-%m-%d").replace(tzinfo=CST)
        except ValueError:
            log(f"[weekly] WEEK_ANCHOR 格式应为 YYYY-MM-DD，收到 {anchor_env!r}")
        if a:
            targets.append(a)
    else:
        if today.isoweekday() == 7:
            targets.append(today)
        targets.append(today - timedelta(days=7))

    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    for anchor in targets:
        key = week_key(anchor)
        days = week_days(anchor)
        is_current = (not anchor_env) and anchor.isoweekday() == 7
        md_path = WEEKLY_DIR / f"{key}.md"
        if md_path.exists() and not is_current:
            continue  # 往周已有就不重写
        top = pick_week(all_d, days, top_n)
        if not top:
            log(f"[weekly] {key}（{days[0]}~{days[-1]}）没有内容，跳过")
            continue
        md = render_md(key, days, top, now)
        md_path.write_text(md, encoding="utf-8")
        data = {
            "week": key,
            "start": days[0],
            "end": days[-1],
            "generated": now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
            "items": top,
        }
        (WEEKLY_DIR / f"{key}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        log(f"[weekly] 已写出 {key}（{days[0]}~{days[-1]}，{len(top)} 条）")


if __name__ == "__main__":
    main()
