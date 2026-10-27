#!/usr/bin/env python3
"""用无头浏览器翻页抓 xbangdan 完整榜单，生成插件用的 tags.json。

为什么不用 requests + BeautifulSoup 直接抓：
  完整榜单的分页、排序都不改 URL，是同页客户端分页。
  首屏 DOM 里只有第 1 页（账号 20 条、长文 20 张、推文左右各 50 条），
  点第 2 页是整批替换数据，不是显隐预载数据。
  所以只抓一个 URL 只能拿到第 1 页，之前快照里今日涨粉、长文作者为 0、
  黑马只有 9 条预览，就是这个原因。MCP 是付费的（999 元/年），
  且未验证能返回完整榜单，不走那条路；也不凭空猜 /api/ 地址。

数据标签只剩两个（2026-10-27 用户定稿，关系类 6 个标签另见
relationship-tags.md，不受影响）：
  - 推文榜 https://xbangdan.com/posts/
    左右两列各自翻页（增速、曝光各最多 3 页），去重后的号 -> 今日爆款
  - 黑马筛选页 https://xbangdan.com/accounts/?rising=1
    网站已经做了判定和筛选：默认就是 5000 粉以下的新锐号，
    页里出现的号就直接贴黑马，不再自己加一道粉丝/帖均量化兜底重打分。
已删除：今日涨粉（账号榜涨粉排序）、长文作者（长文榜 12 页）、
全部赛道标签（AI、加密、搞钱、科技、时事的关键词打分）。
删的理由：长文和涨粉遇到率低、与爆款/黑马重叠大，赛道关键词是
我们自己在打分而不是网站的判定，且时事这类标签几乎人人都有。

依赖：playwright（浏览器）、beautifulsoup4（解析）。
  pip install playwright beautifulsoup4
  playwright install chromium
工作流里必须装 playwright 并装 chromium，否则本脚本会直接退出、
不覆盖旧 tags.json，绝不静默退回只抓第 1 页的旧逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_xbangdan import (  # noqa: E402
    HEADERS,
    handle_from_href,
    parse_number,
)

BASE = "https://xbangdan.com"
ACCOUNTS_RISING = f"{BASE}/accounts/?rising=1"
POSTS_URL = f"{BASE}/posts/"


def parse_accounts_html(html: str) -> list[dict]:
    """解析账号榜表格页（单页）。以表头为准定列序，不固定列位置。

    表格列示例（顺序会随排序变）：
      账号 | 今天推文曝光 | 粉丝/今天涨粉 | 今天发帖 | 今天评论 | 7天帖均曝光/最高单帖
    粉丝/涨粉格如 "3.5万 +2651"；帖均格如 "11.9万 峰 142.5万"。
    """
    soup = BeautifulSoup(html, "html.parser")
    headers = [th.get_text(" ", strip=True) for th in soup.find_all("th")]
    col = {}
    for i, h in enumerate(headers):
        if "涨粉" in h or "粉丝" in h:
            col["follow"] = i
        elif "帖均" in h:
            col["avg"] = i
        elif "曝光" in h:
            col.setdefault("exposure", i)
        elif "发帖" in h:
            col["posts"] = i
        elif "评论" in h:
            col["comments"] = i
    rows = []
    for tr in soup.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            h = handle_from_href(a["href"])
            if h:
                link = (h, a)
                break
        if not link:
            continue
        handle, _ = link
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if not cells:
            continue
        # 表头和单元格可能差一位（账号列、操作列），用“单元格含 handle 链接”
        # 的位置对齐：直接按单元格文本匹配，而不是死记下标。
        metrics: dict = {}
        # 粉丝/涨粉：找形如 "3.5万 +2651" 或 "2743 +2" 的格子
        for text in cells:
            m = re.search(r"([\d,\.]+[万亿千]?)\s*([+-]-?\d+)\s*$", text)
            if m:
                followers = parse_number(m.group(1))
                if followers is not None:
                    metrics["followers"] = followers
                    metrics["follow_gain"] = float(m.group(2))
                break
        # 帖均/峰：找形如 "11.9万 峰 142.5万" 的格子
        for text in cells:
            m = re.search(r"([\d,\.]+[万亿千]?)\s*峰\s*([\d,\.]+[万亿千]?)", text)
            if m:
                avg = parse_number(m.group(1))
                peak = parse_number(m.group(2))
                if avg is not None:
                    metrics["avg_exposure"] = avg
                if peak is not None:
                    metrics["peak_exposure"] = peak
                break
        # 其余指标按表头列序取，取不到就跳过，不硬猜。
        def cell_for(key: str) -> str | None:
            idx = col.get(key)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx]

        for key, mkey in [("exposure", "exposure"), ("posts", "posts"),
                          ("comments", "comments")]:
            text = cell_for(key)
            if text:
                v = parse_number(re.sub(r"[^\d,\.万亿千]", "", text))
                if v is not None and mkey not in metrics:
                    metrics[mkey] = v
        rows.append({"handle": handle, "metrics": metrics})
    return rows


def parse_posts_html(html: str) -> dict[str, dict]:
    """解析推文榜单页（单页，两列合在一起）。复用旧脚本的卡片逻辑。"""
    from fetch_xbangdan import parse_posts_page

    return parse_posts_page(html)


def next_button(page):
    """找“下一页”按钮，找不到或已禁用返回 None。"""
    for locator in [
        page.locator("button", has_text="下一页"),
        page.locator("a", has_text="下一页"),
        page.get_by_text("下一页", exact=False),
    ]:
        try:
            if locator.count() and locator.first.is_enabled():
                return locator.first
        except Exception:
            continue
    return None


def first_handle_of(page_content: str) -> str | None:
    soup = BeautifulSoup(page_content, "html.parser")
    for a in soup.find_all("a", href=True):
        h = handle_from_href(a["href"])
        if h:
            return h
    return None


def collect_pages(page, url: str, parse_fn, max_pages: int,
                  stop_fn=None, label: str = "") -> list:
    """翻页采集单列表页。返回每页解析结果的列表（按页顺序）。

    每次点下一页后等首个号变化，超时或按钮消失即停，绝不死循环。
    """
    print(f"[fetch] {label} {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    results = []
    for page_no in range(1, max_pages + 1):
        html = page.content()
        parsed = parse_fn(html)
        results.append(parsed)
        print(f"[parse] {label} 第 {page_no} 页：{len(parsed)} 条")
        if stop_fn and stop_fn(parsed):
            print(f"[parse] {label} 达到停止条件，停在第 {page_no} 页")
            break
        if page_no >= max_pages:
            break
        before = first_handle_of(html)
        btn = next_button(page)
        if btn is None:
            print(f"[parse] {label} 没有下一页按钮，停在第 {page_no} 页")
            break
        try:
            btn.click(timeout=10000)
        except Exception as exc:
            print(f"[parse] {label} 点下一页失败：{exc}，停在第 {page_no} 页")
            break
        page.wait_for_timeout(2000)
        after_html = page.content()
        if before and first_handle_of(after_html) == before:
            print(f"[parse] {label} 翻页后首条未变，视为已到末页")
            break
    return results


def collect_posts_pages(page, max_pages_per_list: int = 3) -> dict[str, dict]:
    """推文榜左右两列各自翻页。两列的下一页按钮是独立的，按顺序各点各的。

    若按钮定位失败，退回只用首屏数据并打印警告，不让整次运行失败。
    """
    print(f"[fetch] 推文榜 {POSTS_URL}")
    page.goto(POSTS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    merged: dict[str, dict] = {}
    order = 0

    def merge_one(parsed: dict[str, dict]):
        nonlocal order
        for key, entry in parsed.items():
            if key not in merged:
                order += 1
                merged[key] = {
                    "handle": entry["handle"],
                    "display_name": entry["display_name"],
                    "rank_posts": order,
                    "metrics": {},
                }
            for k, v in entry.get("metrics", {}).items():
                merged[key]["metrics"].setdefault(k, v)

    merge_one(parse_posts_html(page.content()))
    # 左右两列的下一页按钮：按页面上出现的顺序取前两个，分别点。
    try:
        btns = page.locator("button", has_text="下一页")
        count = btns.count()
    except Exception:
        count = 0
    print(f"[parse] 推文榜下一页按钮数：{count}")
    for btn_idx in range(min(count, 2)):
        for _ in range(max_pages_per_list - 1):
            try:
                btn = page.locator("button", has_text="下一页").nth(btn_idx)
                if not btn.is_enabled():
                    break
                before = page.content()
                btn.click(timeout=10000)
                page.wait_for_timeout(2000)
                after = page.content()
                if first_handle_of(after) == first_handle_of(before):
                    break
                merge_one(parse_posts_html(after))
            except Exception as exc:
                print(f"[parse] 推文榜第 {btn_idx + 1} 列翻页失败：{exc}")
                break
    print(f"[parse] 推文榜去重后共 {len(merged)} 个号")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tags.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[error] 未安装 playwright，无法翻页抓完整榜单；"
              "请在工作流里 pip install playwright 并执行 playwright install chromium",
              file=sys.stderr)
        return 2

    posts: dict[str, dict] = {}
    rising_rows: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            # 1. 推文榜（两列翻页）-> 今日爆款
            try:
                posts = collect_posts_pages(page)
            except Exception as exc:
                print(f"[error] 推文榜抓取失败：{exc}", file=sys.stderr)
            # 2. 黑马筛选页，39 页全翻，页里出现的号都算黑马
            try:
                pages = collect_pages(page, ACCOUNTS_RISING, parse_accounts_html,
                                      max_pages=39, label="黑马榜")
                for rows in pages:
                    rising_rows.extend(rows)
            except Exception as exc:
                print(f"[error] 黑马榜抓取失败：{exc}", file=sys.stderr)
        finally:
            browser.close()

    merged: dict[str, dict] = {}

    def get_entry(key: str, handle: str) -> dict:
        return merged.setdefault(key, {
            "handle": handle, "display_name": handle, "tags": [], "metrics": {},
        })

    # 推文榜 -> 今日爆款（全量去重，不再封顶 100）
    for key, entry in posts.items():
        out = get_entry(key, entry["handle"])
        out["display_name"] = entry["display_name"] or out["display_name"]
        for k, v in entry["metrics"].items():
            out["metrics"].setdefault(k, v)
        out["tags"].append("今日爆款")

    # 黑马：网站黑马页里出现的号直接贴，不再自己设粉丝/帖均门槛
    for row in rising_rows:
        key = row["handle"].lower()
        out = get_entry(key, row["handle"])
        for k, v in row["metrics"].items():
            out["metrics"].setdefault(k, v)
        if "黑马" not in out["tags"]:
            out["tags"].append("黑马")

    accounts = {k: v for k, v in merged.items() if v["tags"]}
    for v in accounts.values():
        seen: list[str] = []
        for t in v["tags"]:
            if t not in seen:
                seen.append(t)
        v["tags"] = seen[:2]

    hot = sum(1 for v in accounts.values() if "今日爆款" in v["tags"])
    black_n = sum(1 for v in accounts.values() if "黑马" in v["tags"])
    print(f"[result] 有标签的号 {len(accounts)} 个，"
          f"其中今日爆款 {hot} 个、黑马 {black_n} 个")

    if len(accounts) < 20 or hot < 10 or black_n < 10:
        print("[error] 解析到的号低于阈值，拒绝覆盖旧数据", file=sys.stderr)
        return 2

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "xbangdan.com",
        "source_pages": [POSTS_URL, ACCOUNTS_RISING],
        "accounts": accounts,
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        return 0
    tmp_path = args.out + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(tmp_path, encoding="utf-8") as f:
        json.load(f)
    if os.path.exists(args.out):
        os.replace(args.out, args.out + ".prev")
    os.replace(tmp_path, args.out)
    print(f"[ok] 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
