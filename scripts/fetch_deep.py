#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WSJ 深度分析文筛选器。

与 wsj-daily-brief 的区别：daily 追求「全而快」，本脚本追求「深而慢」——
用 Bing News RSS 的 description（真实摘要）+ 标题句式 + 栏目 + 存活时长
多信号打分，从全站条目里把分析型长文挑出来。

数据源：Bing News RSS（site:wsj.com 分板块查询）。
WSJ 文章正文在付费墙后（直连 401，r.jina.ai / allorigins 代理均无效），
只能拿到标题 + 官方摘要，正文需订阅后自行打开链接。

用法:
    python3 fetch_deep.py                        # 默认 72 小时窗口，输出 JSON
    python3 fetch_deep.py --hours 168 --top 30
    python3 fetch_deep.py --min-score 40
    python3 fetch_deep.py --md                   # Markdown 清单
    python3 fetch_deep.py --no-history           # 不读写存活历史
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CST = timezone(timedelta(hours=8))
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(SKILL_DIR, ".history.json")
HISTORY_KEEP_DAYS = 30

# 板块 -> Bing 查询。DEEP 权重高的栏目排在前面。
SECTIONS = {
    # 天然深度栏目
    "arts-culture": ("site%3Awsj.com%2Farts-culture", 12),
    "style": ("site%3Awsj.com%2Fstyle", 12),
    "health": ("site%3Awsj.com%2Fhealth", 10),
    "investing": ("site%3Awsj.com%2Ffinance%2Finvesting", 10),
    "c-suite": ("site%3Awsj.com%2Fbusiness%2Fc-suite", 10),
    "china": ("site%3Awsj.com%2Fworld%2Fchina", 8),
    # 常规栏目（分析文也常出现）
    "opinion": ("site%3Awsj.com%2Fopinion", 10),
    "tech": ("site%3Awsj.com%2Ftech", 10),
    "economy": ("site%3Awsj.com%2Feconomy", 8),
    "business": ("site%3Awsj.com%2Fbusiness", 4),
    "markets": ("site%3Awsj.com%2Fmarkets", 4),
    "finance": ("site%3Awsj.com%2Ffinance", 4),
    "world": ("site%3Awsj.com%2Fworld", 4),
    "politics": ("site%3Awsj.com%2Fpolitics", 0),
    "us-news": ("site%3Awsj.com%2Fus-news", -8),
    "lifestyle": ("site%3Awsj.com%2Flifestyle", 2),
    "science": ("site%3Awsj.com%2Fscience", 6),
    "sports": ("site%3Awsj.com%2Fsports", -6),
}

# 分析性标题开头
ANALYSIS_LEAD = re.compile(
    r"^\s*(how|why|what|where|when|who|inside|the truth about|"
    r"the case for|the case against|can|could|should|is|are|does|did|"
    r"will|would|the end of|the rise of|the fall of|the future of|"
    r"the myth of|the problem with|what.*means)\b", re.I)

# 突发/简讯信号词（命中即降权）
BREAKING = re.compile(
    r"\b(dies|dead|killed|crash(?:es|ed)?|says|said|pledges?|falls?|"
    r"rises?|rose|wins?|won|hits?|strikes?|cuts?|slides?|jumps?|"
    r"sinks?|soars?|plunges?|surges?|slumps?|edges?|settles?|"
    r"arrested|charged|sues?|settlement|recalls?|explodes?|shooting)\b", re.I)

# 明确的非深度模板（直接剔除）
EXCLUDE = re.compile(
    r"^(news quiz|notable\s*&?\s*quotable|print edition|week ahead|"
    r"live updates|what's news|morning briefing|stocks to watch|"
    r"opinion:\s*|letters to the editor)", re.I)


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _real_url(link: str) -> str:
    """Bing 的 link 是 apiclick 跳转，真实地址藏在 url 参数里。
    同时剥掉 ?mod=xxx 之类的站内追踪参数，否则同一篇文章会被算作两条。"""
    if not link:
        return ""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if "url" in q:
            url = urllib.parse.unquote(q["url"][0])
        elif "www.bing.com" in urllib.parse.urlparse(link).netloc:
            url = link
        else:
            url = link
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(p._replace(query="", fragment=""))
    except Exception:
        pass
    return link


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _strip_html(s: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", s or ""))


def fetch_section(name: str, query: str, weight: int, retries: int = 2) -> list:
    url = f"https://www.bing.com/news/search?q={query}&format=RSS&count=50"
    for attempt in range(retries + 1):
        try:
            root = ET.fromstring(_get(url))
            out = []
            for item in root.findall(".//item"):
                link = _real_url(item.findtext("link"))
                pub = item.findtext("pubDate")
                try:
                    dt = parsedate_to_datetime(pub).astimezone(CST)
                except Exception:
                    dt = None
                out.append({
                    "title": _clean(re.sub(r"\s*-\s*wsj\.com\s*$", "",
                                           item.findtext("title") or "", flags=re.I)),
                    "summary": _strip_html(item.findtext("description")),
                    "url": link,
                    "section": name,
                    "section_weight": weight,
                    "pub_cst": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                    "pub_iso": dt.isoformat() if dt else "",
                })
            return out
        except Exception as e:
            if attempt == retries:
                print(f"[warn] {name} failed: {e}", file=sys.stderr)
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


def load_history() -> dict:
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(hist: dict, today: str):
    cutoff = (datetime.now(CST) - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for url, rec in hist.items():
        days = [d for d in rec.get("days", []) if d >= cutoff]
        if days:
            out[url] = {"days": sorted(set(days)), "title": rec.get("title", "")}
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


# 内容类型判定
REVIEW = re.compile(r"(review:\s|:\s*[‘']?[A-Z][^’']{2,60}[’']?\s+review)|"
                    r"(^review:\s)", re.I)
FIRSTPERSON = re.compile(r"\b(i spent|i flew|i tried|i went|my |we spent)\b", re.I)


def content_type(item: dict) -> str:
    """review / opinion / analysis / feature / news"""
    t, s, sec = item["title"], item["summary"], item["section"]
    if REVIEW.search(t) or re.search(r"\breview\b", t, re.I) and sec in (
            "arts-culture", "style"):
        return "review"
    if sec == "opinion":
        return "opinion"
    if ANALYSIS_LEAD.match(t) and not BREAKING.search(t):
        return "analysis"
    if re.search(r"\b(inside|the hunt for|the messy drama|secret|"
                 r"untold|behind the)\b", t, re.I) or FIRSTPERSON.search(t):
        return "feature"
    return "news"


def score(item: dict, hist: dict, today: str) -> dict:
    """多信号深度打分，返回 (score, signals)。"""
    t, s = item["title"], item["summary"]
    sig, score = [], 0

    # 0. 伪摘要：Bing 有时只把标题复读一遍当 description——直接出局
    if s and t and (s.lower().startswith(t[:40].lower()) and len(s) - len(t) < 60):
        return {"score": 0, "signals": ["伪摘要"], "drop": True}

    # 1. 摘要长度——有实质摘要是深度文最硬的可见信号
    n = len(s)
    if n >= 200:
        score += 25; sig.append("摘要充分")
    elif n >= 120:
        score += 16; sig.append("摘要中等")
    elif n >= 60:
        score += 8
    elif n > 0:
        score += 2; sig.append("摘要极短")
    else:
        score -= 10; sig.append("无摘要")

    # 2. 分析性标题句式
    if ANALYSIS_LEAD.match(t):
        score += 18; sig.append("分析式提问")

    # 3. 突发词降权
    if BREAKING.search(t):
        score -= 20; sig.append("突发/行情型")

    # 4. 标题长度（分析文标题通常更长）
    w = len(t.split())
    if w >= 9:
        score += 8; sig.append("长标题")
    elif w <= 4:
        score -= 6; sig.append("短标题")

    # 5. 冒号结构（WSJ 分析文常见 "X: Y"）
    if ": " in t:
        score += 6; sig.append("副标题结构")

    # 6. 栏目权重
    sw = item.get("section_weight", 0)
    score += sw
    if sw >= 10:
        sig.append("深度栏目")
    elif sw < 0:
        sig.append("快讯栏目")

    # 7. 存活时长（跨天仍被推 = 长尾深度文）
    rec = hist.get(item["url"])
    days = rec.get("days", []) if rec else []
    if len(days) >= 3:
        score += 18; sig.append(f"存活{len(days)}天")
    elif len(days) == 2:
        score += 10; sig.append("存活2天")

    # 8. 摘要含「报道/分析/解释」类引导词
    if re.search(r"\b(explains?|analysis|why|how|report(?:s|ed)?|"
                 r"examines?|argues?|finds?)\b", s, re.I):
        score += 6

    return {"score": max(0, min(100, score + 30)), "signals": sig}  # +30 拉到正区间


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72, help="时间窗，默认 72，0=不过滤")
    ap.add_argument("--min-score", type=int, default=45)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--sections", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--exclude", default="",
                    help="剔除类型，逗号分隔：review/opinion/analysis/feature/news")
    args = ap.parse_args()

    sections = {k: v for k, v in SECTIONS.items()
                if not args.sections or k in args.sections.split(",")}

    rows = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda kv: fetch_section(kv[0], kv[1][0], kv[1][1]),
                        sections.items()):
            rows.extend(r)

    # 去重（同 URL 保留摘要最长的那条）
    merged = {}
    for r in rows:
        if not r["title"] or EXCLUDE.match(r["title"]):
            continue
        k = r["url"] or r["title"]
        if k not in merged or len(r["summary"]) > len(merged[k]["summary"]):
            merged[k] = r
    uniq = list(merged.values())

    if args.hours > 0:
        cutoff = datetime.now(CST) - timedelta(hours=args.hours)
        uniq = [r for r in uniq
                if r["pub_iso"] and datetime.fromisoformat(r["pub_iso"]) >= cutoff]

    today = datetime.now(CST).strftime("%Y-%m-%d")
    hist = {} if args.no_history else load_history()

    for r in uniq:
        r["type"] = content_type(r)
        r.update(score(r, hist, today))

    # 硬过滤：伪摘要 / 无实质摘要（深度文几乎都有像样的摘要）
    uniq = [r for r in uniq if not r.get("drop") and len(r["summary"]) >= 70]

    if args.exclude:
        drop = set(x.strip() for x in args.exclude.split(","))
        uniq = [r for r in uniq if r["type"] not in drop]

    uniq = [r for r in uniq if r["score"] >= args.min_score]
    uniq.sort(key=lambda r: (-r["score"], r["pub_iso"]), reverse=False)
    uniq = uniq[:args.top]

    # 更新存活历史
    if not args.no_history:
        for r in uniq:
            rec = hist.setdefault(r["url"], {"days": [], "title": r["title"]})
            if today not in rec["days"]:
                rec["days"].append(today)
            rec["title"] = r["title"]
        save_history(hist, today)

    meta = {
        "fetched_at_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "source": "Bing News RSS (site:wsj.com)",
        "window_hours": args.hours,
        "min_score": args.min_score,
        "total_scanned": len(merged),
        "kept": len(uniq),
        "note": "正文在付费墙后（直连 401），仅标题+官方摘要；深度靠多信号打分，最终由 LLM 复核",
    }

    if args.md:
        lines = [f"# WSJ 深度候选 {meta['fetched_at_cst']} · "
                 f"扫描 {meta['total_scanned']} 条 / 入围 {len(uniq)} 条"]
        for r in uniq:
            lines.append(
                f"\n## [{r['score']}] {r['title']}\n"
                f"- 板块 {r['section']} · {r['pub_cst']} · 信号：{'/'.join(r['signals'])}\n"
                f"- 摘要：{r['summary'][:400]}\n"
                f"- {r['url']}")
        text = "\n".join(lines)
    else:
        text = json.dumps({"meta": meta, "items": uniq},
                          ensure_ascii=False, indent=1)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[ok] 扫描 {meta['total_scanned']} / 入围 {len(uniq)} -> {args.out}",
              file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
