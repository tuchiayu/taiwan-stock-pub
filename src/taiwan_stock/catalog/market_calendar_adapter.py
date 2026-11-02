"""TWSE website calendar and explicit, full-market closure announcements."""

import hashlib
import json
import re
from datetime import date
from html import unescape

import httpx2

ROOT = "https://www.twse.com.tw"
ANNUAL_URL = ROOT + "/rwd/zh/holidaySchedule/holidaySchedule"
NEWS_URL = ROOT + "/rwd/zh/news/newsList"
DETAIL_URL = ROOT + "/rwd/zh/news/newsDetail/"
_CLOSURE = re.compile(
    r"臺灣證券交易所集中交易市場(\d{2,3})年(\d{1,2})月(\d{1,2})日"
    r"(?:全日)?休市(?:一天|一日)[。！]?"
)


def clean(value):
    return unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def request(client, url, **params):
    response = client.get(url, params=params or None)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("stat") != "ok":
        raise ValueError("TWSE 行事曆來源狀態異常")
    return data


def table(data, fields):
    if data.get("fields") != fields or not isinstance(data.get("data"), list):
        raise ValueError("TWSE 行事曆欄位異常")
    if any(not isinstance(r, list) or len(r) != len(fields) for r in data["data"]):
        raise ValueError("TWSE 行事曆資料列異常")
    return data["data"]


def annual(client, year):
    raw = request(client, ANNUAL_URL, response="json", date=f"{year}0101")
    if raw.get("queryYear") != year or raw.get("date") != f"{year}0101":
        raise ValueError("TWSE 回傳行事曆年份不符")
    rows = table(raw, ["日期", "名稱", "說明"])
    events, reasons = [], []
    seen = set()
    for day, name, description in rows:
        day = date.fromisoformat(day).isoformat()
        if not day.startswith(f"{year}-") or day in seen:
            raise ValueError("TWSE 行事曆日期年份或重複資料異常")
        seen.add(day)
        name, description = clean(name), clean(description)
        text = name + description
        if any(word in text for word in ("不交易", "無交易", "休市", "放假", "補假")):
            state = "closed"
        elif any(word in text for word in ("開始交易", "最後交易", "補行交易")):
            state = "open"
        else:
            state = "needs_review"
            reasons.append("年度表有未辨識的市場狀態")
        events.append(
            event(day, day, state, name, name, ANNUAL_URL + f"?date={year}0101", None)
        )
    return events, raw, reasons


def event(key, day, state, name, title, url, published):
    return {
        "key": key,
        "date": day,
        "status": state,
        "name": name,
        "title": title,
        "url": url,
        "published": published,
        "fingerprint": fingerprint([title, day, state, url, published]),
    }


def notices(client, year):
    events, reasons, snapshots, seen = [], [], [], set()
    for published_year in (year - 1, year):
        raw = request(
            client,
            NEWS_URL,
            response="json",
            tag="休市",
            startDate=f"{published_year}0101",
            endDate=f"{published_year}1231",
        )
        rows = table(raw, ["項次", "標題", "日期", "zhId", "enId"])
        if type(raw.get("totalCount")) is not int or raw["totalCount"] != len(rows):
            raise ValueError("TWSE 休市公告清單未完整取得")
        snapshots.append(raw)
        for _, title, published, news_id, _ in rows:
            title, published = clean(title), clean(published)
            stamp = re.fullmatch(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", published)
            if not stamp:
                raise ValueError("TWSE 公告發布日期異常")
            stamp = date(int(stamp[1]) + 1911, int(stamp[2]), int(stamp[3]))
            if stamp.year != published_year:
                raise ValueError("TWSE 公告查詢年份不符")
            if not isinstance(news_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9]{1,64}", news_id
            ):
                raise ValueError("TWSE 公告識別異常")
            if news_id in seen:
                raise ValueError("TWSE 公告重複")
            seen.add(news_id)
            compact = re.sub(r"\s+", "", title)
            match = _CLOSURE.fullmatch(compact)
            if not match:
                if "休市" in title and "集中交易市場" in title:
                    reasons.append("有全市場休市公告待確認")
                    dates = re.findall(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", compact)
                    for y, m, d in dates:
                        uncertain = date(int(y) + 1911, int(m), int(d))
                        if uncertain.year == year:
                            events.append(
                                event(
                                    news_id,
                                    uncertain.isoformat(),
                                    "needs_review",
                                    "休市資訊待確認",
                                    title,
                                    ROOT + "/zh/news/content.html?" + news_id,
                                    stamp.isoformat(),
                                )
                            )
                continue
            day = date(int(match[1]) + 1911, int(match[2]), int(match[3]))
            if day.year != year:
                continue
            try:
                detail = request(client, DETAIL_URL + news_id)
            except httpx2.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                reasons.append("休市公告詳情已無法取得，需確認")
                events.append(
                    event(
                        news_id,
                        day.isoformat(),
                        "needs_review",
                        "休市資訊待確認",
                        title,
                        ROOT + "/zh/news/content.html?" + news_id,
                        stamp.isoformat(),
                    )
                )
                continue
            tables = detail.get("tables")
            if not isinstance(tables, list) or not tables:
                raise ValueError("TWSE 休市公告詳情異常")
            head = tables[0]
            if re.sub(r"\s+", "", clean(head.get("title"))) != compact:
                raise ValueError("TWSE 休市公告標題不符")
            state = "closed"
            if head.get("fields") != ["text", "html", "pdf", "lang"]:
                raise ValueError("TWSE 休市公告詳情欄位異常")
            content = head.get("data")
            if not isinstance(content, list) or len(content) != 4:
                raise ValueError("TWSE 休市公告詳情資料異常")
            # Content changes are reviewed even if the list title stayed the same.
            item = event(
                news_id,
                day.isoformat(),
                state,
                "臨時休市",
                title,
                ROOT + "/zh/news/content.html?" + news_id,
                stamp.isoformat(),
            )
            item["fingerprint"] = fingerprint([title, detail])
            events.append(item)
            snapshots.append(detail)
    return events, snapshots, reasons
