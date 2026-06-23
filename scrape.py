#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九州イベント情報スクレイパー
----------------------------------
対象サイトを巡回し、対象エリア（福岡県南部・熊本県北部・佐賀）の
直近イベントを抽出して README.md を生成する。

GitHub Actions から毎週実行される想定。
HTML 構造は予告なく変わるため、各パーサは「壊れても落ちない」設計にし、
取得できなかったサイトはスキップして処理を継続する。
"""

import re
import sys
import datetime
import traceback

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

JST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(JST).date()

# 抽出する期間（今日の数日前〜今日から約2ヶ月先）
WINDOW_PAST = datetime.timedelta(days=3)
WINDOW_FUTURE = datetime.timedelta(days=60)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

# エリア判定キーワード（市町村名 → エリア）
AREA_KEYWORDS = {
    "福岡県南部": [
        "久留米", "大牟田", "筑後", "八女", "柳川", "大川", "みやま",
        "小郡", "広川", "大木", "うきは", "大刀洗", "恋ぼたる",
    ],
    "熊本県北部": [
        "玉名", "山鹿", "荒尾", "南関", "長洲", "和水", "玉東",
        "菊水", "平山温泉", "鹿央",
    ],
    "佐賀エリア": [
        "佐賀", "鳥栖", "みやき", "神埼", "吉野ヶ里", "基山", "上峰",
    ],
}

# どのエリアにも当てはまらないが収集はしておきたい熊本県内ワード（その他）
OTHER_KUMAMOTO = [
    "熊本市", "阿蘇", "高森", "人吉", "球磨", "天草", "八代", "宇土",
    "水俣", "芦北", "菊池", "御船", "甲佐",
]

AREA_ORDER = ["福岡県南部", "熊本県北部", "佐賀エリア", "その他"]


# ---------------------------------------------------------------------------
# 日付パース
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日")
MD_RE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日")


def parse_dates(text):
    """テキストから (start_date, end_date, 表示用文字列) を推定して返す。

    見つからない場合は (None, None, "") を返す。
    """
    if not text:
        return None, None, ""

    full = DATE_RE.findall(text)
    dates = []
    for y, m, d in full:
        try:
            dates.append(datetime.date(int(y), int(m), int(d)))
        except ValueError:
            continue

    # 「6月27日」のように年が省略された日付も拾う（年は今年/来年で補完）
    if not dates:
        for m, d in MD_RE.findall(text):
            try:
                m, d = int(m), int(d)
                year = TODAY.year
                cand = datetime.date(year, m, d)
                # 過去すぎる場合は翌年扱い
                if cand < TODAY - datetime.timedelta(days=120):
                    cand = datetime.date(year + 1, m, d)
                dates.append(cand)
            except ValueError:
                continue

    if not dates:
        return None, None, ""

    start = min(dates)
    end = max(dates)

    if start == end:
        disp = f"{start.month}/{start.day}"
    else:
        disp = f"{start.month}/{start.day}〜{end.month}/{end.day}"
    return start, end, disp


def in_window(start, end):
    """イベント期間が抽出ウィンドウに重なるか判定。"""
    if start is None:
        return False
    lo = TODAY - WINDOW_PAST
    hi = TODAY + WINDOW_FUTURE
    e = end or start
    # 期間 [start, e] と [lo, hi] が重なるか
    return start <= hi and e >= lo


def classify_area(text):
    """テキストからエリアを判定。該当なしは None。"""
    for area, kws in AREA_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return area
    for kw in OTHER_KUMAMOTO:
        if kw in text:
            return "その他"
    return None


# ---------------------------------------------------------------------------
# 取得ユーティリティ
# ---------------------------------------------------------------------------

def fetch(url, timeout=30):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


# ---------------------------------------------------------------------------
# サイト別パーサ
# 返り値: list[dict] 各 dict は title/date_disp/start/end/place/detail/url のキーを持つ
# ---------------------------------------------------------------------------

def parse_chikugo_ikoi():
    """筑後いこい（WordPress, サーバーレンダリング）。"""
    url = "https://chikugo-ikoi.com/category/event/"
    events = []
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    for art in soup.find_all("article"):
        # タイトルリンク: 投稿本文へのリンク（カテゴリ "イベント" リンクは除外）
        title_a = None
        for a in art.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if not txt or txt == "イベント":
                continue
            if "/category/" in a["href"]:
                continue
            title_a = a
            break
        if title_a is None:
            continue

        title = title_a.get_text(strip=True)
        link = title_a["href"]

        # 概要テキスト（article 内の最長テキストブロック）
        blocks = [t.strip() for t in art.stripped_strings]
        summary = max(blocks, key=len) if blocks else ""

        haystack = title + " " + summary
        start, end, disp = parse_dates(summary) or (None, None, "")
        if start is None:
            start, end, disp = parse_dates(title)

        area = classify_area(haystack)
        events.append({
            "title": title,
            "url": link,
            "place": extract_place(title, summary),
            "detail": short(summary),
            "start": start, "end": end, "date_disp": disp,
            "area": area,
            "source": "筑後いこい",
        })
    return events


def parse_kumamoto_guide():
    """くまもとガイド 公式観光サイト。県北系エリアの検索結果を収集。"""
    events = []
    # area=6:県北, 64:荒尾・玉名, 65:山鹿
    for area_id in ("6", "64", "65"):
        url = f"https://kumamoto.guide/events/search?area%5B0%5D={area_id}"
        try:
            html = fetch(url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/events/detail/" not in href:
                continue
            block = a.get_text(" ", strip=True)
            if not block:
                # 親要素にタイトルがある場合
                parent = a.find_parent(["li", "div", "article"])
                block = parent.get_text(" ", strip=True) if parent else ""
            if not block:
                continue
            start, end, disp = parse_dates(block)
            # タイトルは日付・エリア表記を除いた先頭部分を推定
            title = clean_title(block)
            full = href if href.startswith("http") else "https://kumamoto.guide" + href
            events.append({
                "title": title,
                "url": full,
                "place": "",
                "detail": short(block),
                "start": start, "end": end, "date_disp": disp,
                "area": classify_area(block) or "熊本県北部",
                "source": "くまもとガイド",
            })
    return dedupe(events)


def parse_nishinippon():
    """西日本新聞 九州イベント。福岡市中心のためエリアキーワードで絞り込む。"""
    url = "https://www.nishinippon.co.jp/kyushu_event/"
    events = []
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"/kyushu_event/\d+/?$", href):
            continue
        title = a.get_text(strip=True)
        if not title or title == "詳細を見る":
            continue
        parent = a.find_parent(["article", "li", "div"])
        ctx = parent.get_text(" ", strip=True) if parent else title
        area = classify_area(title + " " + ctx)
        if area is None:
            continue  # 対象エリア外はスキップ
        start, end, disp = parse_dates(ctx)
        full = href if href.startswith("http") else "https://www.nishinippon.co.jp" + href
        events.append({
            "title": title,
            "url": full,
            "place": "",
            "detail": short(ctx),
            "start": start, "end": end, "date_disp": disp,
            "area": area,
            "source": "西日本新聞",
        })
    return dedupe(events)


# ---------------------------------------------------------------------------
# 整形ヘルパ
# ---------------------------------------------------------------------------

def short(text, n=40):
    text = re.sub(r"\s+", "", text or "")
    return text[:n] + ("…" if len(text) > n else "")


def clean_title(block):
    """日付・エリア注記を取り除いてタイトルらしき先頭を取り出す。"""
    t = re.split(r"20\d{2}年", block)[0].strip()
    t = re.sub(r"^(開催中|NEW!?)", "", t).strip()
    return t[:60] if t else block[:60]


def extract_place(title, summary):
    m = re.search(r"福岡県[^、。\s]+?(?:市|町|村)", summary or "")
    if m:
        return m.group(0).replace("福岡県", "")
    m = re.search(r"（([^）]+?(?:市|町|村))）", title or "")
    if m:
        return m.group(1)
    return ""


def dedupe(events):
    seen = set()
    out = []
    for e in events:
        key = e["url"]
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


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
    ("筑後いこい", "https://chikugo-ikoi.com/category/event/"),
    ("くまもとガイド（公式観光サイト）", "https://kumamoto.guide/events/"),
    ("西日本新聞 九州おでかけ・イベント", "https://www.nishinippon.co.jp/kyushu_event/"),
    ("久留米ファン イベントカレンダー", "https://kurumefan.com/event-calendar-table"),
]


def build_readme(events):
    lines = []
    lines.append("# 九州イベント情報まとめ\n")
    lines.append("福岡県南部・熊本県北部・佐賀を中心とした九州のイベント情報を、"
                 "毎週自動で収集・掲載しています。\n")
    lines.append(f"**最終更新: {TODAY.isoformat()}（自動更新／GitHub Actions）**\n")

    lines.append("**収集サイト一覧:**\n")
    for name, url in SOURCES:
        lines.append(f"- [{name}]({url})")
    lines.append("")
    lines.append("---\n")

    # エリアごとに開始日順で並べる
    by_area = {a: [] for a in AREA_ORDER}
    for e in events:
        area = e.get("area")
        if area in by_area:
            by_area[area].append(e)

    total = 0
    for area in AREA_ORDER:
        items = by_area[area]
        if not items:
            continue
        items.sort(key=lambda x: (x["start"] or datetime.date.max))
        lines.append(f"## {SECTION_TITLES[area]}\n")
        lines.append("| イベント名 | 日程 | 場所 | 詳細 | 出典 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for e in items:
            title = e["title"].replace("|", "｜")
            name_cell = f"[{title}]({e['url']})"
            date_cell = e["date_disp"] or "不明"
            place_cell = e["place"] or "不明"
            detail_cell = (e["detail"] or "").replace("|", "｜")
            src_cell = e["source"]
            lines.append(f"| {name_cell} | {date_cell} | {place_cell} | {detail_cell} | {src_cell} |")
            total += 1
        lines.append("")

    if total == 0:
        lines.append("> 今回の収集では対象期間・対象エリアのイベントが見つかりませんでした。\n")

    lines.append("---\n")
    lines.append("> ⚠️ 参加費・駐車場などの詳細は各イベントの公式情報をご確認ください。"
                 "サイト構造の変化により一部情報が欠落・古くなる場合があります。\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

PARSERS = [
    ("筑後いこい", parse_chikugo_ikoi),
    ("くまもとガイド", parse_kumamoto_guide),
    ("西日本新聞", parse_nishinippon),
]


def main():
    all_events = []
    for name, fn in PARSERS:
        try:
            evs = fn()
            print(f"[OK] {name}: {len(evs)} 件取得", file=sys.stderr)
            all_events.extend(evs)
        except Exception as e:
            print(f"[WARN] {name} の取得に失敗: {e}", file=sys.stderr)
            traceback.print_exc()

    # フィルタ: 対象エリアあり & 期間内
    filtered = [
        e for e in all_events
        if e.get("area") and in_window(e.get("start"), e.get("end"))
    ]
    filtered = dedupe(filtered)
    print(f"[INFO] 抽出後 {len(filtered)} 件 / 取得合計 {len(all_events)} 件", file=sys.stderr)

    readme = build_readme(filtered)
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print("[DONE] README.md を書き出しました", file=sys.stderr)


if __name__ == "__main__":
    main()
