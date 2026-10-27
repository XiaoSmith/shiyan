#!/usr/bin/env python3
"""
抓取 xbangdan.com 公开榜单，生成插件用的 tags.json。

目标（已拍板，不改）：
  - 只抓 xbangdan.com，不抓 x.com，不用 GitHub 抓取项目替代数据源，
    GitHub 项目只当解析/重试思路的参考。
  - 不搞人工种子名单。抓取失败时不靠人工补，而是靠更稳的抓取 +
    保留上一份好数据 + 插件端缓存降级。

数据流：
  GitHub Actions 每天定时跑本脚本
    -> 生成 tags.json（原子替换，失败不覆盖）
    -> 插件从 GitHub Pages / jsDelivr 拉 tags.json，按 @handle 匹配打标签

注意：
  本脚本是 v1 解析骨架。xbangdan 的真实 HTML 结构以第一次真实运行为准，
  如果榜单区块的选择器变了，优先调 parse_home_page() 里的区块关键词，
  而不是改整体流程。解析结果低于阈值时脚本会直接失败退出，
  Actions 就不会用一份空/残缺数据覆盖上一份好数据。

依赖：requests, beautifulsoup4
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://xbangdan.com"
PAGES = [
    ("posts", f"{BASE}/posts/", "推文榜"),
    ("home", f"{BASE}/", "首页榜单"),
]

HEADERS = {
    # 表明身份、不伪装成浏览器用户，频率压到每天 2 次。
    # 注意：HTTP 请求头必须是纯 ASCII，requests 会按 latin-1 编码，
    # 这里放中文（比如“榜单同步”）会在 Actions 里直接报编码错误导致抓取失败。
    "User-Agent": "xbangdan-tags-bot/1.0 (daily-sync; low-frequency)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# x.com 上不是用户名的保留路径，解析 handle 时要排除。
RESERVED_PATHS = {
    "home", "explore", "search", "notifications", "messages", "settings",
    "login", "signup", "i", "intent", "hashtag", "topics", "lists",
    "communities", "spaces", "jobs", "about", "tos", "privacy",
}
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# 首页区块关键词 -> 标签。只认这些词，避免把广告、推荐位误当成榜单。
SECTION_TAGS = [
    ("黑马榜", "黑马"),
    ("长文榜", "长文作者"),
    ("今日涨粉", "今日涨粉"),
    ("涨粉", "今日涨粉"),
]

# 赛道关键词词典（本地规则，不靠人工名单，按帖子文本打分）。
# 只有最高分 >= 2 且明显高于第二名时才贴，宁缺毋滥。
TRACK_KEYWORDS = {
    "AI": ["GPT", "Claude", "LLM", "大模型", "Agent", "智能体", "AI ", "人工智能", "Muse", "Codex"],
    "加密": ["BTC", "ETH", "BNB", "比特币", "以太坊", "Web3", "链上", "币安", "空投"],
    "搞钱": ["副业", "变现", "一人公司", "搞钱", "赚钱", "小红书", "获客", "GMV"],
    "科技": ["开源", "GitHub", "编程", "程序员", "ComfyUI", "工作流", "MCP"],
    "时事": ["姆巴佩", "金球奖", "A股", "财报", "突发"],
}

MIN_TOTAL_ACCOUNTS = 20   # 低于这个数就判定为解析失败，不覆盖旧数据
MIN_HOT_ACCOUNTS = 10     # 推文榜至少要解析出这么多号


def parse_number(text: str) -> float | None:
    """'22.5万' -> 225000，'1.2亿' -> 120000000，'1,234' -> 1234。"""
    if not text:
        return None
    text = text.replace(",", "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([万亿千]?)$", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        num *= 10_000
    elif unit == "亿":
        num *= 100_000_000
    elif unit == "千":
        num *= 1_000
    return num


def handle_from_href(href: str) -> str | None:
    if not href:
        return None
    try:
        parsed = urlparse(href)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in ("x.com", "twitter.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    candidate = parts[0]
    if candidate.lower() in RESERVED_PATHS:
        return None
    if not HANDLE_RE.match(candidate):
        return None
    return candidate


def handle_from_avatar(src: str) -> str | None:
    """xbangdan 首页很多头像是 /avatars/<handle>.jpg，这是一个交叉验证信号。"""
    if not src or "/avatars/" not in src:
        return None
    name = src.split("/avatars/")[-1].split("/")[0].split("?")[0]
    name = re.sub(r"\.(jpg|jpeg|png|webp)$", "", name, flags=re.I)
    if HANDLE_RE.match(name):
        return name
    return None


def fetch_html(url: str, timeout: int = 30) -> str:
    """带重试的抓取：3 次，间隔 5s / 10s，任何一次成功即返回。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            if len(resp.text) < 1000:
                raise ValueError(f"页面内容过短 ({len(resp.text)} 字节)，疑似被拦截或改版")
            return resp.text
        except Exception as exc:  # noqa: BLE001 - 重试逻辑需要兜底
            last_error = exc
            print(f"[fetch] {url} 第 {attempt}/3 次失败：{exc}", file=sys.stderr)
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(f"抓取 {url} 3 次均失败：{last_error}")


def nearby_metrics_text(tag) -> str:
    """取链接最近的容器文本，用于提取曝光/点赞/粉丝数等指标。

    只往上走两级、且绝不走到 body/html，避免把整页文本当成某个号的指标
    （之前测试里这正是导致后一个号复用前一个号指标的 bug）。
    """
    parent = tag.parent
    for _ in range(2):
        if parent is None or parent.name in ("body", "html", "[document]"):
            return ""
        text = parent.get_text(" ", strip=True)
        if text:
            return text[:500]
        parent = parent.parent
    return ""


def extract_metrics(text: str) -> dict:
    metrics: dict[str, float] = {}
    # 先提帖均曝光并从文本里去掉，避免“11.9万帖均”被误读成发帖数 11.9 万。
    m = re.search(r"([\d,\.]+[万亿千]?)\s*帖均", text)
    if m:
        value = parse_number(m.group(1))
        if value is not None:
            metrics["avg_exposure"] = value
        text = text.replace(m.group(0), " ")
    for label, key in [("曝光", "exposure"), ("赞", "likes"), ("粉丝", "followers"),
                       ("帖", "posts"), ("评论", "comments"), ("收藏", "bookmarks")]:
        m = re.search(rf"([\d,\.]+[万亿千]?)\s*{label}", text)
        if m:
            value = parse_number(m.group(1))
            if value is not None:
                metrics[key] = value
    return metrics


def parse_posts_page(html: str) -> dict[str, dict]:
    """推文榜：按出现顺序排，顺序即榜单顺序，前 100 名贴今日爆款。"""
    soup = BeautifulSoup(html, "html.parser")
    accounts: dict[str, dict] = {}
    order = 0
    for a in soup.find_all("a", href=True):
        handle = handle_from_href(a["href"])
        if not handle:
            continue
        key = handle.lower()
        if key not in accounts:
            order += 1
            accounts[key] = {
                "handle": handle,
                "display_name": a.get_text(" ", strip=True)[:50] or handle,
                "rank_posts": order,
                "texts": [],
                "metrics": {},
            }
        text = nearby_metrics_text(a)
        if text and text not in accounts[key]["texts"]:
            accounts[key]["texts"].append(text)
            accounts[key]["metrics"].update(extract_metrics(text))
    return accounts


def parse_home_page(html: str) -> dict[str, dict]:
    """首页：尽量按区块关键词分流；分流失败的号只记录出现，不乱贴标签。"""
    soup = BeautifulSoup(html, "html.parser")
    accounts: dict[str, dict] = {}
    current_tag: str | None = None

    # 先按文档顺序扫一遍标题类元素，记录当前区块。
    # 注意：这里不依赖具体 class，只依赖区块标题文本，改样式不怕，改标题才需要改词表。
    for el in soup.find_all(True):
        text = el.get_text(" ", strip=True) if el.name in ("h1", "h2", "h3", "h4") else ""
        if text:
            for keyword, tag in SECTION_TAGS:
                if keyword in text:
                    current_tag = tag
                    break
        if el.name == "a" and el.has_attr("href"):
            handle = handle_from_href(el["href"])
            if not handle:
                continue
            key = handle.lower()
            entry = accounts.setdefault(key, {
                "handle": handle,
                "display_name": el.get_text(" ", strip=True)[:50] or handle,
                "sections": [],
                "texts": [],
                "metrics": {},
            })
            if current_tag and current_tag not in entry["sections"]:
                entry["sections"].append(current_tag)
            text = nearby_metrics_text(el)
            if text and text not in entry["texts"]:
                entry["texts"].append(text)
                entry["metrics"].update(extract_metrics(text))

    # 头像文件名交叉验证：补上链接里没出现的号（只补，不单独成标签依据）。
    for img in soup.find_all("img", src=True):
        handle = handle_from_avatar(img["src"])
        if handle:
            accounts.setdefault(handle.lower(), {
                "handle": handle,
                "display_name": handle,
                "sections": [],
                "texts": [],
                "metrics": {},
            })
    return accounts


def classify_track(texts: list[str]) -> str | None:
    joined = " ".join(texts)
    scores: dict[str, int] = {}
    lowered = joined.lower()
    for track, keywords in TRACK_KEYWORDS.items():
        score = 0
        for kw in keywords:
            # 英文不区分大小写计一次，中文按原文计一次，避免重复计数。
            score += lowered.count(kw.lower()) if kw.isascii() else joined.count(kw)
        scores[track] = score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if not ranked or ranked[0][1] < 2:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 1:
        return None
    return ranked[0][0]


def build_tags(posts: dict, home: dict) -> dict:
    merged: dict[str, dict] = {}

    def get_entry(handle_key: str, handle: str) -> dict:
        return merged.setdefault(handle_key, {
            "handle": handle,
            "display_name": handle,
            "tags": [],
            "metrics": {},
        })

    # 推文榜 -> 今日爆款（前 100 名）
    for key, entry in posts.items():
        out = get_entry(key, entry["handle"])
        out["display_name"] = entry["display_name"] or out["display_name"]
        out["metrics"].update(entry["metrics"])
        if entry.get("rank_posts") and entry["rank_posts"] <= 100:
            out["tags"].append("今日爆款")
        track = classify_track(entry["texts"])
        if track:
            out["tags"].append(track)

    # 首页区块 -> 对应标签
    for key, entry in home.items():
        out = get_entry(key, entry["handle"])
        out["display_name"] = entry["display_name"] or out["display_name"]
        out["metrics"].update(entry["metrics"])
        for section in entry["sections"]:
            if section not in out["tags"]:
                out["tags"].append(section)
        # 黑马的量化兜底：粉丝 < 5000 且帖均曝光 >= 20000
        followers = entry["metrics"].get("followers")
        avg = entry["metrics"].get("avg_exposure")
        if followers is not None and avg is not None and followers < 5000 and avg >= 20000:
            if "黑马" not in out["tags"]:
                out["tags"].append("黑马")

    # 只保留确实有标签的号，避免把“只出现过一次”也发给插件。
    accounts = {k: v for k, v in merged.items() if v["tags"]}
    # 标签去重保序，并限制每号最多 3 个（赛道只占 1 个位置）。
    for v in accounts.values():
        seen: list[str] = []
        for t in v["tags"]:
            if t not in seen:
                seen.append(t)
        v["tags"] = seen[:3]
    return accounts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tags.json", help="输出文件，默认 tags.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    parser.add_argument("--min-accounts", type=int, default=MIN_TOTAL_ACCOUNTS)
    args = parser.parse_args()

    all_data: dict[str, dict] = {}
    posts = {}
    home = {}
    for page_key, url, label in PAGES:
        print(f"[fetch] {label} {url}")
        html = fetch_html(url)
        time.sleep(1)  # 页面之间停 1 秒，不 hammer 对方服务器
        if page_key == "posts":
            posts = parse_posts_page(html)
        else:
            home = parse_home_page(html)
        print(f"[parse] {label}: 解析到 {len(posts if page_key == 'posts' else home)} 个号")
        all_data[page_key] = True

    accounts = build_tags(posts, home)
    hot = sum(1 for v in accounts.values() if "今日爆款" in v["tags"])
    print(f"[result] 有标签的号 {len(accounts)} 个，其中今日爆款 {hot} 个")

    # 校验：低于阈值 = 解析失败，绝不覆盖旧文件，Actions 直接报错告警。
    if len(accounts) < args.min_accounts:
        print(f"[error] 解析到的号只有 {len(accounts)} 个，低于阈值 {args.min_accounts}，拒绝覆盖旧数据",
              file=sys.stderr)
        return 2
    if hot < MIN_HOT_ACCOUNTS:
        print(f"[error] 今日爆款只有 {hot} 个，低于阈值 {MIN_HOT_ACCOUNTS}，拒绝覆盖旧数据",
              file=sys.stderr)
        return 2

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "xbangdan.com",
        "source_pages": [url for _, url, _ in PAGES],
        "accounts": accounts,
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        return 0

    # 原子替换：先写临时文件，校验能解析后再替换；旧文件留一份备份。
    tmp_path = args.out + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(tmp_path, encoding="utf-8") as f:
        json.load(f)  # 校验 JSON 合法
    if os.path.exists(args.out):
        os.replace(args.out, args.out + ".prev")
    os.replace(tmp_path, args.out)
    print(f"[ok] 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
