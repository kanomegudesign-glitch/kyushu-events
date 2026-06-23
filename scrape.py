#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九州イベント情報スクレイパー（Playwright版 / Claude非依存）
==========================================================
GitHub Actions 上のヘッドレスブラウザ（Chromium）で各サイトを巡回し、
対象エリア（福岡県南部・熊本県北部・佐賀）の直近イベントを抽出して
README.md を生成する。Claude や手元の Chrome には一切依存しない。

設計方針:
- 各サイトの HTML 構造は変わりうるため、サイトごとのパーサは
  「壊れても落ちない」ようにし、取れなかったサイトはスキップして継続する。
- JS で描画されるサイトもブラウザでレンダリングしてから解析する。
- 精度より堅牢性を優先。エリア判定キーワード＋日付ウィンドウでノイズを除去する。
"""

import re
import sys
import datetime
import traceback

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

JST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(JST).date()

WINDOW_PAST = datetime.timedelta(days=1)     # 終了済みは基本除外（前日まで許容）
WINDOW_FUTURE = datetime.timedelta(days=60)  # 約2ヶ月先まで

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# エリア判定キーワード（市町村名 → エリア）
AREA_KEYWORDS = {
    "福岡県南部": [
        "久留米", "大牟田", "筑後", "八女", "柳川", "大川", "みやま",
        "小郡", "広川", "大木", "うきは", "大刀洗", "恋ぼたる", "筑紫野",
    ],
    "熊本県北部": [
        "玉名", "山鹿", "荒尾", "南関", "長洲", "和水", "玉東",
        "菊水", "平山温泉", "鹿央",
    ],
    "佐賀エリア": [
        "佐賀", "鳥栖", "みやき", "神埼", "吉野ヶ里", "基山", "上峰",
    ],
}
OTHER_KUMAMOTO = [
    "熊本市", "阿蘇", "高森", "人吉", "球磨", "天草", "八代", "宇土",
    "水俣", "芦北", "菊池", "御船", "甲佐",
]
AREA_ORDER = ["福岡県南部", "熊本県北部", "佐賀エリア", "その他"]

# 全国チェーン/投票系など、地域固有でないノイズを弾く語
EXCLUDE_WORDS = [
    "総選挙", "人気投票", "ランキングTOP", "オンライン", "通販",
]

# ---------------------------------------------------------------------------
# 日付パース
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"(20\d{2})\s*[./年]\s*(\d{1,2})\s*[./月]\s*(\d{1,2})")
MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def parse_dates(text):
    if not text:
        return None, None, ""
    dates = []
    for y, m, d in DATE_RE.findall(text):
        try:
            dates.append(datetime.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if not dates:
        for m, d in MD_RE.findall(text):
            try:
                m, d = int(m), int(d)
                cand = datetime.date(TODAY.year, m, d)
                if cand < TODAY - datetime.timedelta(days=120):
                    cand = datetime.date(TODAY.year + 1, m, d)
                dates.append(cand)
            except ValueError:
                pass
    if not dates:
        return None, None, ""
    start, end = min(dates), max(dates)
    disp = f"{start.month}/{start.day}" if start == end else \
           f"{start.month}/{start.day}〜{end.month}/{end.day}"
    return start, end, disp


def in_window(start, end):
    if start is None:
        return False
    lo, hi = TODAY - WINDOW_PAST, TODAY + WINDOW_FUTURE
    e = end or start
    return start <= hi and e >= lo


def classify_area(text):
    for area, kws in AREA_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return area
    for kw in OTHER_KUMAMOTO:
        if kw in text:
            return "その他"
    return None


def is_excluded(text):
    return any(w in text for w in EXCLUDE_WORDS)


# ---------------------------------------------------------------------------
# 整形ヘルパ
# ---------------------------------------------------------------------------

def short(text, n=42):
    text = re.sub(r"\s+", "", text or "")
    return text[:n] + ("…" if len(text) > n else "")


def clean_title(text):
    t = re.split(r"20\d{2}年", text)[0].strip()
    t = re.sub(r"^(開催中|NEW!?|PR)", "", t).strip()
    return (t or text)[:70]


def extract_place(title, summary):
    m = re.search(r"(福岡県|熊本県|佐賀県)?[^、。\s（）]{0,8}?(市|町|村)", summary or "")
    if m:
        return m.group(0).replace("福岡県", "").replace("熊本県", "").replace("佐賀県", "")
    m = re.search(r"（([^）]+?(?:市|町|村))）", title or "")
    if m:
        return m.group(1)
    return ""


def dedupe(events):
    seen, out = set(), []
    for e in events:
        key = (e.get("title", "")[:24], e.get("date_disp", ""))
        if e["url"] in seen or key in seen:
            continue
        seen.add(e["url"])
        seen.add(key)
        out.append(e)
    return out


def make_event(title, url, ctx, source, area_hint=None):
    title = (title or "").strip()
    if not title:
        return None
    haystack = title + " " + (ctx or "")
    if is_excluded(haystack):
        return None
    start, end, disp = parse_dates(ctx)
    if start is None:
        start, end, disp = parse_dates(title)
    area = classify_area(haystack) or area_hint
    return {
        "title": clean_title(title),
        "url": url,
        "place": extract_place(title, ctx),
        "detail": short(ctx or title),
        "start": start, "end": end, "date_disp": disp,
        "area": area, "source": source,
    }


# ---------------------------------------------------------------------------
# 汎用抽出: レンダリング済み HTML からアンカー＋近傍日付でイベント候補を拾う
# ---------------------------------------------------------------------------

def parse_generic(html, base_url, source, area_hint=None, href_filter=None):
    events = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or len(title) < 4:
            continue
        if href_filter and href_filter not in href:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        # 近傍テキスト（最も近い記事/リスト要素）
        container = a.find_parent(["article", "li", "section", "div"])
        ctx = container.get_text(" ", strip=True) if container else title
        ctx = ctx[:400]
        if not (DATE_RE.search(ctx) or MD_RE.search(ctx)):
            continue
        full = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
        ev = make_event(title, full, ctx, source, area_hint)
        if ev and ev["area"]:
            events.append(ev)
    events = dedupe(events)
    # 開催日が近い順に並べて上限件数で打ち切る
    events.sort(key=lambda x: (x["start"] or datetime.date.max))
    return events[:MAX_PER_SOURCE]


# ---------------------------------------------------------------------------
# サイト別パーサ（構造が判明しているもの）
# ---------------------------------------------------------------------------

def parse_chikugo_ikoi(html):
    events = []
    soup = BeautifulSoup(html, "html.parser")
    for art in soup.find_all("article"):
        title_a = None
        for a in art.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if not txt or txt == "イベント" or "/category/" in a["href"]:
                continue
            title_a = a
            break
        if not title_a:
            continue
        blocks = [t.strip() for t in art.stripped_strings]
        summary = max(blocks, key=len) if blocks else ""
        ev = make_event(title_a.get_text(strip=True), title_a["href"],
                        summary, "筑後いこい", "福岡県南部")
        if ev and ev["area"]:
            events.append(ev)
    return dedupe(events)


def parse_kumamoto_guide_area(html):
    events = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if "/events/detail/" not in a["href"]:
            continue
        block = a.get_text(" ", strip=True)
        if not block:
            p = a.find_parent(["li", "div", "article"])
            block = p.get_text(" ", strip=True) if p else ""
        if not block:
            continue
        full = a["href"] if a["href"].startswith("http") else "https://kumamoto.guide" + a["href"]
        ev = make_event(block, full, block, "くまもとガイド", "熊本県北部")
        if ev:
            events.append(ev)
    return dedupe(events)


# ---------------------------------------------------------------------------
# サイト設定: (ラベル, URL, パーサ種別, 追加情報)
# ---------------------------------------------------------------------------

KUMAMOTO_AREAS = ["6", "64", "65"]  # 県北・荒尾玉名・山鹿

GENERIC_SITES = [
    # (source名, URL, base_url, area_hint, href_filter)
    # ※ href_filter はリンクが相対パスの場合も一致するよう、ドメインではなくパス断片を使う
    ("久留米ファン", "https://kurumefan.com/event-calendar-table",
     "https://kurumefan.com", "福岡県南部", None),
    ("イオンモール大牟田", "https://omuta.aeonmall.jp/event",
     "https://omuta.aeonmall.jp", "福岡県南部", "/event"),
    ("イオンモール筑紫野", "https://chikushino.aeonmall.jp/event",
     "https://chikushino.aeonmall.jp", "福岡県南部", "/event"),
    ("イオン小郡SC", "https://tenpo.aeon-kyushu.info/detail/ogori/",
     "https://tenpo.aeon-kyushu.info", "福岡県南部", None),
    ("熊本おでかけ情報", "https://kumamoto-odekake.com/event/",
     "https://kumamoto-odekake.com", None, "/event/"),
    ("西日本新聞", "https://www.nishinippon.co.jp/kyushu_event/",
     "https://www.nishinippon.co.jp", None, "/kyushu_event/"),
    ("サンリオスポット", "https://www.sanrio.co.jp/spots/?categories=56",
     "https://www.sanrio.co.jp", None, None),
]

# 1サイトが大量のリンクを拾ってREADMEを埋め尽くさないよう、サイト単位で上限を設ける
MAX_PER_SOURCE = 30


def render(page, url, wait_ms=2500):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(wait_ms)
    return page.content()


# ---------------------------------------------------------------------------
# README 生成
# ---------------------------------------------------------------------------

SECTION_TITLES = {
    "福岡県南部": "📍 福岡県南部（久留米・大牟田・筑後・八女・柳川・みやま・小郡など）",
    "熊本県北部": "📍 熊本県北部（荒尾・玉名・山鹿・南関など）",
    "佐賀エリア": "📍 佐賀エリア（佐賀市・鳥栖市・みやき町など）",
    "その他": "📍 その他（熊本市・阿蘇・人吉球磨など）",
}
SOURCES = [
    ("西日本新聞 福岡おでかけ・イベント情報", "https://www.nishinippon.co.jp/kyushu_event/"),
    ("久留米ファン イベントカレンダー", "https://kurumefan.com/event-calendar-table"),
    ("筑後いこい", "https://chikugo-ikoi.com/category/event/"),
    ("くまもとガイド（公式観光サイト）", "https://kumamoto.guide/events/"),
    ("熊本おでかけ情報", "https://kumamoto-odekake.com/event/"),
    ("サンリオ スポット", "https://www.sanrio.co.jp/spots/?categories=56"),
    ("イオンモール大牟田", "https://omuta.aeonmall.jp/event"),
    ("イオンモール筑紫野", "https://chikushino.aeonmall.jp/event"),
    ("イオン小郡ショッピングセンター", "https://tenpo.aeon-kyushu.info/detail/ogori/"),
]


def build_readme(events):
    L = []
    L.append("# 九州イベント情報まとめ\n")
    L.append("福岡県南部・熊本県北部・佐賀を中心とした九州のイベント情報を、"
             "毎日自動で収集・掲載しています。\n")
    L.append(f"**最終更新: {TODAY.isoformat()}（自動更新／GitHub Actions・Claude非依存）**\n")
    L.append("**収集サイト一覧:**\n")
    for name, url in SOURCES:
        L.append(f"- [{name}]({url})")
    L.append("")
    L.append("---\n")

    by_area = {a: [] for a in AREA_ORDER}
    for e in events:
        if e.get("area") in by_area:
            by_area[e["area"]].append(e)

    total = 0
    for area in AREA_ORDER:
        items = by_area[area]
        if not items:
            continue
        items.sort(key=lambda x: (x["start"] or datetime.date.max))
        L.append(f"## {SECTION_TITLES[area]}\n")
        L.append("| イベント名 | 日程 | 場所 | 詳細 | 出典 |")
        L.append("| --- | --- | --- | --- | --- |")
        for e in items:
            name = f"[{e['title'].replace('|', '｜')}]({e['url']})"
            L.append(f"| {name} | {e['date_disp'] or '不明'} | {e['place'] or '不明'} "
                     f"| {(e['detail'] or '').replace('|', '｜')} | {e['source']} |")
            total += 1
        L.append("")

    if total == 0:
        L.append("> 今回の収集では対象期間・対象エリアのイベントが見つかりませんでした。\n")
    L.append("---\n")
    L.append("> ⚠️ 参加費・駐車場などの詳細は各イベントの公式情報をご確認ください。"
             "サイト構造の変化により一部情報が欠落する場合があります。\n")
    return "\n".join(L), total


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    all_events = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, locale="ja-JP",
                                  viewport={"width": 1366, "height": 1800})
        page = ctx.new_page()

        # 1) 筑後いこい
        try:
            html = render(page, "https://chikugo-ikoi.com/category/event/")
            evs = parse_chikugo_ikoi(html)
            print(f"[OK] 筑後いこい: {len(evs)}", file=sys.stderr)
            all_events += evs
        except Exception as e:
            print(f"[WARN] 筑後いこい: {e}", file=sys.stderr); traceback.print_exc()

        # 2) くまもとガイド（エリア別検索）
        for aid in KUMAMOTO_AREAS:
            try:
                url = f"https://kumamoto.guide/events/search?area%5B0%5D={aid}"
                html = render(page, url)
                evs = parse_kumamoto_guide_area(html)
                print(f"[OK] くまもとガイド area={aid}: {len(evs)}", file=sys.stderr)
                all_events += evs
            except Exception as e:
                print(f"[WARN] くまもとガイド area={aid}: {e}", file=sys.stderr)

        # 3) 汎用サイト
        for source, url, base, hint, hf in GENERIC_SITES:
            try:
                html = render(page, url)
                evs = parse_generic(html, base, source, hint, hf)
                print(f"[OK] {source}: {len(evs)}", file=sys.stderr)
                all_events += evs
            except Exception as e:
                print(f"[WARN] {source}: {e}", file=sys.stderr)

        browser.close()

    filtered = [e for e in all_events
                if e.get("area") and in_window(e.get("start"), e.get("end"))]
    filtered = dedupe(filtered)
    readme, total = build_readme(filtered)
    print(f"[INFO] 抽出 {total} 件 / 取得合計 {len(all_events)} 件", file=sys.stderr)

    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print("[DONE] README.md を書き出しました", file=sys.stderr)


if __name__ == "__main__":
    main()
