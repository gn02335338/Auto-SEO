#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import calendar
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

import pandas as pd
import requests
import feedparser
from dateutil import parser as dtparser

# -------------------------
# 可調整參數
# -------------------------

LOOKBACK_DAYS_DEFAULT = 90
REQUEST_TIMEOUT = 15
MAX_ENTRIES_EVAL = 500

# 權重（依你的要求）
WEIGHT_VOLUME = 0.30
WEIGHT_RECENCY = 0.60
WEIGHT_FREQUENCY = 0.05
WEIGHT_REGULARITY = 0.05

# 若想把權重正規化成總和=1，設 True
NORMALIZE_WEIGHTS = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 頻率 outlier 防護
DEFAULT_MIN_INTERVAL_DAYS = 0.5            # 夾到至少 0.5 天，避免 1/極小值爆大
DEFAULT_IGNORE_INTERVAL_BELOW_MINUTES = 1  # 忽略 <1 分鐘的相鄰間隔（多為同批發布/重複時間戳）

# -------------------------
# 資料結構
# -------------------------

@dataclass
class FeedMetrics:
    ok: bool
    http_status: Optional[int]
    feed_title: Optional[str]
    total_items: int
    items_lookback: int
    latest_pub_at: Optional[datetime]
    latest_age_days: Optional[float]
    mean_interval_days_raw: Optional[float]
    mean_interval_days_clamped: Optional[float]
    std_interval_days: Optional[float]
    intervals_count_raw: int
    intervals_count_used: int
    # 分數/相對量
    volume_score: Optional[float] = None
    recency_score: Optional[float] = None
    frequency_score: Optional[float] = None
    regularity_score: Optional[float] = None
    final_score: Optional[float] = None
    frequency_rate: Optional[float] = None  # = 1 / mean_interval_days_clamped
    error: Optional[str] = None

# -------------------------
# 工具函式
# -------------------------

def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def parse_struct_time(ts) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)
    except Exception:
        return None

def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed", "created_parsed", "expired_parsed"):
        if key in entry and entry[key]:
            dt = parse_struct_time(entry[key])
            if dt:
                return dt
    for key in ("published", "updated", "created", "issued"):
        s = entry.get(key)
        if s:
            try:
                dt = dtparser.parse(s)
                return to_utc(dt)
            except Exception:
                continue
    return None

def safe_get(url: str) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://rss.app/",
    }
    return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

# -------------------------
# 單一 feed 計算（第一階段）
# -------------------------

def analyze_feed(rss_url: str, lookback_days: int, min_interval_days: float, ignore_interval_below_minutes: float) -> FeedMetrics:
    now = datetime.now(timezone.utc)
    lookback_from = now - timedelta(days=lookback_days)

    try:
        resp = safe_get(rss_url)
        status = resp.status_code

        # 簡單擋爬/非 XML 檢測
        ctype = resp.headers.get("Content-Type", "").lower()
        head = resp.text[:400].lower()
        looks_blocked = (status >= 400) or (("xml" not in ctype) and ("<html" in head))
        if looks_blocked:
            return FeedMetrics(
                ok=False, http_status=status, feed_title=None,
                total_items=0, items_lookback=0, latest_pub_at=None,
                latest_age_days=None, mean_interval_days_raw=None, mean_interval_days_clamped=None,
                std_interval_days=None, intervals_count_raw=0, intervals_count_used=0,
                error=f"Blocked or non-XML content-type: {ctype or 'N/A'}"
            )

        parsed = feedparser.parse(resp.content)
        feed_title = (parsed.feed.get("title") if hasattr(parsed, "feed") else None) or None
        entries = parsed.entries or []
        entries = entries[:MAX_ENTRIES_EVAL]

        pub_times: List[datetime] = []
        for e in entries:
            dt = parse_entry_datetime(e)
            if dt:
                pub_times.append(to_utc(dt))
        pub_times.sort()

        total_items = len(entries)
        latest_pub_at = pub_times[-1] if pub_times else None
        latest_age_days = ((now - latest_pub_at).total_seconds() / 86400.0) if latest_pub_at else None
        items_lookback = sum(1 for dt in pub_times if dt >= lookback_from)

        # 原始間隔
        intervals_raw: List[float] = []
        for i in range(1, len(pub_times)):
            d = (pub_times[i] - pub_times[i - 1]).total_seconds() / 86400.0
            if d > 0:
                intervals_raw.append(d)

        # 過濾「過小間隔」
        min_minutes = max(0.0, ignore_interval_below_minutes)
        intervals_used = [d for d in intervals_raw if (d * 1440.0) >= min_minutes]

        intervals_count_raw = len(intervals_raw)
        intervals_count_used = len(intervals_used)

        mean_interval_days_raw = (sum(intervals_used) / len(intervals_used)) if intervals_used else None

        # 標準差（用過濾後的 intervals）
        if intervals_used and len(intervals_used) >= 2:
            mean_tmp = mean_interval_days_raw or 0.0
            var = sum((x - mean_tmp) ** 2 for x in intervals_used) / (len(intervals_used) - 1)
            std_interval_days = math.sqrt(var)
        else:
            std_interval_days = None

        # 夾下限避免 1/極小值爆大
        if mean_interval_days_raw and mean_interval_days_raw > 0:
            mean_interval_days_clamped = max(mean_interval_days_raw, min_interval_days)
            frequency_rate = 1.0 / mean_interval_days_clamped
        else:
            mean_interval_days_clamped = None
            frequency_rate = 0.0

        # Recency：今天有文=1，否則線性遞減到 lookback 為 0
        if latest_age_days is None:
            recency_score = 0.0
        elif latest_age_days <= 1.0:  # 同日內都視為滿分
            recency_score = 1.0
        else:
            recency_score = clamp01(1.0 - (latest_age_days / lookback_days))

        # Regularity：1 - std/(2*mean)
        if mean_interval_days_raw and mean_interval_days_raw > 0:
            if std_interval_days is None:
                regularity_score = 0.5
            else:
                norm = std_interval_days / (2.0 * mean_interval_days_raw)
                regularity_score = clamp01(1.0 - norm)
        else:
            regularity_score = 0.0

        return FeedMetrics(
            ok=True, http_status=status, feed_title=feed_title,
            total_items=total_items, items_lookback=items_lookback,
            latest_pub_at=latest_pub_at, latest_age_days=latest_age_days,
            mean_interval_days_raw=mean_interval_days_raw,
            mean_interval_days_clamped=mean_interval_days_clamped,
            std_interval_days=std_interval_days,
            intervals_count_raw=intervals_count_raw,
            intervals_count_used=intervals_count_used,
            recency_score=recency_score, regularity_score=regularity_score,
            frequency_rate=frequency_rate
        )

    except Exception as e:
        return FeedMetrics(
            ok=False, http_status=None, feed_title=None,
            total_items=0, items_lookback=0, latest_pub_at=None,
            latest_age_days=None, mean_interval_days_raw=None, mean_interval_days_clamped=None,
            std_interval_days=None, intervals_count_raw=0, intervals_count_used=0,
            error=str(e)
        )

# -------------------------
# 二階段相對化 + 總分
# -------------------------

def finalize_scores(metrics_list: List[FeedMetrics]):
    max_items = max((m.items_lookback for m in metrics_list if m.ok), default=0)
    max_rate = max((m.frequency_rate or 0.0 for m in metrics_list if m.ok), default=0.0)

    wv, wr, wf, wg = WEIGHT_VOLUME, WEIGHT_RECENCY, WEIGHT_FREQUENCY, WEIGHT_REGULARITY
    if NORMALIZE_WEIGHTS:
        s = wv + wr + wf + wg
        if s > 0:
            wv, wr, wf, wg = wv / s, wr / s, wf / s, wg / s

    for m in metrics_list:
        if not m.ok:
            m.volume_score = 0.0
            m.frequency_score = 0.0
        else:
            m.volume_score = clamp01(m.items_lookback / max_items) if max_items > 0 else 0.0
            rate = m.frequency_rate or 0.0
            m.frequency_score = clamp01(rate / max_rate) if max_rate > 0 else 0.0

        v = m.volume_score or 0.0
        r = m.recency_score or 0.0
        f = m.frequency_score or 0.0
        g = m.regularity_score or 0.0
        m.final_score = (wv * v) + (wr * r) + (wf * f) + (wg * g)

# -------------------------
# 主程式
# -------------------------

def main():
    ap = argparse.ArgumentParser(description="RSS 品質評分與排名（相對化版，含頻率 outlier 防護）")
    ap.add_argument("--input", required=True, help="輸入 CSV 路徑（含欄位：Main_site,Sub_site,URL,RSS_URL；表頭在第3列）")
    ap.add_argument("--output", default="rss_ranked.csv", help="輸出 CSV 路徑（預設 rss_ranked.csv）")
    ap.add_argument("--header-row-index", type=int, default=2, help="表頭所在的 0-based 列索引（預設 2）")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_DEFAULT, help=f"近期文章量回顧天數（預設 {LOOKBACK_DAYS_DEFAULT}）")
    ap.add_argument("--workers", type=int, default=10, help="並發抓取執行緒數（預設 10）")
    ap.add_argument("--min-interval-days", type=float, default=DEFAULT_MIN_INTERVAL_DAYS, help=f"最小平均間隔天數下限（預設 {DEFAULT_MIN_INTERVAL_DAYS}）")
    ap.add_argument("--ignore-interval-below-minutes", type=float, default=DEFAULT_IGNORE_INTERVAL_BELOW_MINUTES, help=f"忽略小於此分鐘數的相鄰間隔（預設 {DEFAULT_IGNORE_INTERVAL_BELOW_MINUTES} 分鐘）")
    args = ap.parse_args()

    df = pd.read_csv(args.input, header=args.header_row_index)
    df["RSS_URL"] = df["RSS_URL"].astype(str).str.strip()
    df_rss = df[df["RSS_URL"].astype(bool)].copy()
    if df_rss.empty:
        print("沒有可用的 RSS_URL。")
        return

    jobs = {}
    metrics_list: List[FeedMetrics] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for _, row in df_rss.iterrows():
            fut = ex.submit(analyze_feed, row["RSS_URL"], args.lookback_days, args.min_interval_days, args.ignore_interval_below_minutes)
            jobs[fut] = row

        for fut in as_completed(jobs):
            row = jobs[fut]
            m: FeedMetrics = fut.result()
            # 臨時掛來源欄位
            m._Main_site = row.get("Main_site", None)  # type: ignore
            m._Sub_site = row.get("Sub_site", None)    # type: ignore
            m._URL = row.get("URL", None)              # type: ignore
            m._RSS_URL = row.get("RSS_URL", None)      # type: ignore
            metrics_list.append(m)

    finalize_scores(metrics_list)

    rows = []
    for m in metrics_list:
        rows.append({
            "Main_site": getattr(m, "_Main_site", None),
            "Sub_site": getattr(m, "_Sub_site", None),
            "URL": getattr(m, "_URL", None),
            "RSS_URL": getattr(m, "_RSS_URL", None),
            "feed_title": m.feed_title,
            "http_status": m.http_status,
            "fetch_ok": m.ok,
            "error": m.error,
            "total_items": m.total_items,
            f"items_last_{LOOKBACK_DAYS_DEFAULT}d": m.items_lookback,
            "latest_pub_at_utc": m.latest_pub_at.isoformat() if m.latest_pub_at else None,
            "latest_age_days": round(m.latest_age_days, 3) if m.latest_age_days is not None else None,
            "intervals_count_raw": m.intervals_count_raw,
            "intervals_count_used": m.intervals_count_used,
            "mean_interval_days_raw": round(m.mean_interval_days_raw, 6) if m.mean_interval_days_raw is not None else None,
            "mean_interval_days_clamped": round(m.mean_interval_days_clamped, 6) if m.mean_interval_days_clamped is not None else None,
            "std_interval_days": round(m.std_interval_days, 6) if m.std_interval_days is not None else None,
            "frequency_rate(1/days)": round(m.frequency_rate, 6) if m.frequency_rate is not None else None,
            "volume_score": round(m.volume_score or 0.0, 4),
            "recency_score": round(m.recency_score or 0.0, 4),
            "frequency_score": round(m.frequency_score or 0.0, 4),
            "regularity_score": round(m.regularity_score or 0.0, 4),
            "final_score": round(m.final_score or 0.0, 4),
        })

    out_df = pd.DataFrame(rows)
    out_df.sort_values(
        by=["fetch_ok", "final_score", "latest_pub_at_utc"],
        ascending=[False, False, False],
        inplace=True,
        kind="mergesort"
    )

    out_df.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"完成！已輸出：{args.output}")
    if not NORMALIZE_WEIGHTS and abs((WEIGHT_VOLUME+WEIGHT_RECENCY+WEIGHT_FREQUENCY+WEIGHT_REGULARITY) - 1.0) > 1e-6:
        print("注意：目前權重總和不為 1（不影響排名；如需滿分=1，請將 NORMALIZE_WEIGHTS=True）")

if __name__ == "__main__":
    main()
