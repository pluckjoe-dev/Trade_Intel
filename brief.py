#!/usr/bin/env python3
"""Trade Intel market brief generator.

Pulls candles from the eToro Public API, economic events from ForexFactory's
public feed and earnings dates from Nasdaq, then writes BRIEF.md (for an LLM or
a person to read) and brief.json (machine-readable).

Standard library only, so it runs on a bare GitHub Actions runner.

Environment:
  ETORO_API_KEY   eToro Public API key   (x-api-key)
  ETORO_USER_KEY  eToro user key         (x-user-key)

Usage:
  python brief.py                    # live run, writes BRIEF.md and brief.json
  python brief.py --fixture DIR      # offline run from saved candle JSON
"""

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE = "https://public-api.etoro.com/api/v1"
UA = "Mozilla/5.0 (X11; Linux x86_64) TradeIntelBrief/1.0"

# symbol, eToro instrumentId, class, currencies whose events matter,
# min position exposure ($), max leverage long, max leverage short
# Eligibility figures taken from the eToro account on 2026-10-22.
INSTRUMENTS = [
    ("EURUSD", 1, "FX", ["EUR", "USD"], 1000, 30, 30),
    ("GBPUSD", 2, "FX", ["GBP", "USD"], 1000, 30, 30),
    ("USDJPY", 5, "FX", ["USD", "JPY"], 1000, 30, 30),
    ("AUDUSD", 7, "FX", ["AUD", "USD"], 1000, 20, 20),
    ("SPX500", 27, "Index", ["USD"], 1000, 20, 20),
    ("NSDQ100", 28, "Index", ["USD"], 1000, 20, 20),
    ("UK100", 30, "Index", ["GBP"], 1000, 20, 20),
    ("GER40", 32, "Index", ["EUR"], 1000, 20, 20),
    ("GOLD", 18, "Commodity", ["USD"], 1000, 20, 20),
    ("TSLA", 1111, "Stock", ["USD"], 10, 5, 5),
    ("NVDA", 1137, "Stock", ["USD"], 10, 5, 5),
    ("AAPL", 1001, "Stock", ["USD"], 10, 5, 5),
    ("MSFT", 1004, "Stock", ["USD"], 10, 5, 5),
    ("AMZN", 1005, "Stock", ["USD"], 10, 5, 5),
    ("META", 1003, "Stock", ["USD"], 10, 5, 5),
    ("GOOG", 1002, "Stock", ["USD"], 10, 5, 5),
    ("BTC", 100000, "Crypto", [], 10, 1, 0),
    ("ETH", 100001, "Crypto", [], 10, 1, 0),
]
STOCKS = [i[0] for i in INSTRUMENTS if i[2] == "Stock"]
EVENT_CCYS = {"USD", "EUR", "GBP", "JPY", "AUD"}
STOP_ATR = 1.5          # suggested stop distance in daily ATRs
DAILY_N = 150
HOURLY_N = 168


# ---------------------------------------------------------------- fetching

def http_json(url, headers=None, retries=3):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 10)
                time.sleep(min(wait, 60))
                continue
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(3)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise


def etoro_candles(iid, interval, count):
    url = f"{BASE}/market-data/instruments/{iid}/history/candles/asc/{interval}/{count}"
    headers = {
        "x-api-key": os.environ["ETORO_API_KEY"],
        "x-user-key": os.environ["ETORO_USER_KEY"],
        "x-request-id": str(uuid.uuid4()),
    }
    return http_json(url, headers)


def parse_candles(payload):
    groups = payload.get("candles") or []
    if not groups:
        return []
    out = []
    for c in groups[0].get("candles") or []:
        out.append({
            "t": datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")),
            "o": float(c["open"]), "h": float(c["high"]),
            "l": float(c["low"]), "c": float(c["close"]),
        })
    out.sort(key=lambda x: x["t"])
    return out


def load_candles(iid, interval, count, fixture):
    if fixture:
        path = os.path.join(fixture, f"{iid}_{interval}.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return parse_candles(json.load(f))
    return parse_candles(etoro_candles(iid, interval, count))


def ff_events(now):
    events, notes = [], []
    for name in ("thisweek", "nextweek"):
        try:
            data = http_json(f"https://nfs.faireconomy.media/ff_calendar_{name}.json")
        except Exception as e:  # nextweek is often not published until the weekend
            if name == "thisweek":
                notes.append(f"Economic calendar unavailable ({type(e).__name__}).")
            continue
        for e in data:
            try:
                t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except Exception:
                continue
            if e.get("country") not in EVENT_CCYS or e.get("impact") not in ("High", "Medium"):
                continue
            if now - timedelta(hours=1) <= t <= now + timedelta(hours=72):
                events.append({"time": t, "ccy": e["country"], "impact": e["impact"],
                               "title": e["title"], "forecast": e.get("forecast") or "",
                               "previous": e.get("previous") or ""})
    uniq = {(e["time"], e["ccy"], e["title"]): e for e in events}
    return sorted(uniq.values(), key=lambda e: e["time"]), notes


def earnings(now, days=14):
    found, notes = {}, []
    want = set(STOCKS) | {"GOOGL"}
    failures = 0
    for d in range(days):
        day = (now + timedelta(days=d)).date()
        try:
            data = http_json(f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}")
        except Exception:
            failures += 1
            continue
        rows = ((data or {}).get("data") or {}).get("rows") or []
        for r in rows:
            sym = r.get("symbol")
            if sym in want:
                sym = "GOOG" if sym == "GOOGL" else sym
                when = {"time-after-hours": "after close", "time-pre-market": "before open"}.get(r.get("time") or "", "")
                found.setdefault(sym, {"date": day, "time": when})
        time.sleep(0.3)
    if failures == days:
        notes.append("Earnings calendar unavailable.")
    return found, notes


# ---------------------------------------------------------------- maths

def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def atr(candles, n=14):
    if len(candles) < n + 1:
        return None
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def pct(a, b):
    return (a / b - 1) * 100 if b else None


def vol_regime(closes, window=20):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < window + 20:
        return None, None
    vols = [statistics.pstdev(rets[i - window:i]) for i in range(window, len(rets) + 1)]
    cur = vols[-1]
    rank = sum(1 for v in vols if v <= cur) / len(vols) * 100
    if rank >= 90:
        label = "EXTREME"
    elif rank >= 75:
        label = "elevated"
    elif rank <= 25:
        label = "quiet"
    else:
        label = "normal"
    return label, rank


def trend_label(closes, fast, slow):
    f, s = ema(closes, fast), ema(closes, slow)
    if f is None or s is None:
        return "n/a"
    last = closes[-1]
    if f > s and last > f:
        return "up"
    if f < s and last < f:
        return "down"
    return "flat"


def daily_trend(closes):
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 is None or s50 is None:
        return "n/a", s20, s50
    s20_prev = sma(closes[:-5], 20)
    last = closes[-1]
    if last > s20 and s20 > s50 and s20_prev is not None and s20 >= s20_prev:
        return "up", s20, s50
    if last < s20 and s20 < s50 and s20_prev is not None and s20 <= s20_prev:
        return "down", s20, s50
    return "flat", s20, s50


def to_4h(hourly):
    buckets = {}
    for c in hourly:
        key = c["t"].replace(hour=c["t"].hour // 4 * 4, minute=0, second=0, microsecond=0)
        b = buckets.get(key)
        if b is None:
            buckets[key] = dict(c, t=key)
        else:
            b["h"] = max(b["h"], c["h"])
            b["l"] = min(b["l"], c["l"])
            b["c"] = c["c"]
    return [buckets[k] for k in sorted(buckets)]


def hl(candles):
    if not candles:
        return None, None
    return max(c["h"] for c in candles), min(c["l"] for c in candles)


def fmt(x, ref=None):
    """Format a price with sensible precision for its size."""
    if x is None:
        return "n/a"
    r = abs(ref if ref else x)
    if r < 10:
        return f"{x:.5f}"
    if r < 200:
        return f"{x:.3f}"
    if r < 10000:
        return f"{x:.2f}"
    return f"{x:,.0f}"


def fpct(x, signed=True):
    if x is None:
        return "n/a"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


# ---------------------------------------------------------------- analysis

def analyse(inst, daily, hourly, now, events, earn):
    sym, iid, cls, ccys, min_exp, lev_l, lev_s = inst
    res = {"symbol": sym, "class": cls, "ok": False}
    if len(daily) < 60 or len(hourly) < 30:
        res["error"] = "insufficient price history"
        return res

    today = now.date()
    completed = [c for c in daily if c["t"].date() < today]
    today_c = [c for c in daily if c["t"].date() == today]
    price = hourly[-1]["c"]
    last_bar_age_h = (now - hourly[-1]["t"]).total_seconds() / 3600 - 1
    stale = last_bar_age_h > 2

    closes_d = [c["c"] for c in completed] + [price]
    a = atr(completed)
    vlabel, vrank = vol_regime(closes_d)

    t1h = trend_label([c["c"] for c in hourly], 20, 50)
    t4h = trend_label([c["c"] for c in to_4h(hourly)], 10, 20)
    td, s20, s50 = daily_trend(closes_d)
    tfs = [t1h, t4h, td]
    if tfs.count("up") == 3:
        align = "ALIGNED UP"
    elif tfs.count("down") == 3:
        align = "ALIGNED DOWN"
    elif tfs.count("up") == 2 and "down" not in tfs:
        align = "leaning up"
    elif tfs.count("down") == 2 and "up" not in tfs:
        align = "leaning down"
    elif "up" in tfs and "down" in tfs:
        align = "CONFLICTING"
    else:
        align = "no trend"

    prev = completed[-1]
    piv = (prev["h"] + prev["l"] + prev["c"]) / 3
    # ISO weeks
    wk = lambda c: c["t"].isocalendar()[:2]
    this_wk = wk({"t": now})
    prior_week = [c for c in completed if wk(c) != this_wk]
    if prior_week:
        pw_key = wk(prior_week[-1])
        prior_week = [c for c in prior_week if wk(c) == pw_key]
    mon = (now.year, now.month)
    pm_year, pm_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    prior_month = [c for c in completed if (c["t"].year, c["t"].month) == (pm_year, pm_month)]
    last20 = completed[-20:]

    today_h = max([c["h"] for c in today_c] + [c["h"] for c in hourly if c["t"].date() == today] or [price])
    today_l = min([c["l"] for c in today_c] + [c["l"] for c in hourly if c["t"].date() == today] or [price])
    today_range_atr = (today_h - today_l) / a if a else None

    def sess(h0, h1):
        bars = [c for c in hourly if c["t"].date() == today and h0 <= c["t"].hour < h1]
        return hl(bars)

    levels = {
        "today_high": today_h, "today_low": today_l,
        "prev_day_high": prev["h"], "prev_day_low": prev["l"], "prev_day_close": prev["c"],
        "pivot": piv, "R1": 2 * piv - prev["l"], "S1": 2 * piv - prev["h"],
    }
    pwh, pwl = hl(prior_week)
    if pwh:
        levels.update(prev_week_high=pwh, prev_week_low=pwl)
    h20, l20 = hl(last20)
    levels.update(high_20d=h20, low_20d=l20)
    pmh, pml = hl(prior_month)
    if pmh:
        levels.update(prev_month_high=pmh, prev_month_low=pml)
    levels.update(sma20=s20, sma50=s50)

    # nearest levels above/below in ATR units
    above = sorted(((k, v) for k, v in levels.items() if v and v > price), key=lambda kv: kv[1])
    below = sorted(((k, v) for k, v in levels.items() if v and v < price), key=lambda kv: -kv[1])

    last5 = completed[-5:]
    if td == "up" or align in ("ALIGNED UP", "leaning up"):
        inval = ("long bias invalid on a daily close below", min(c["l"] for c in last5))
    elif td == "down" or align in ("ALIGNED DOWN", "leaning down"):
        inval = ("short bias invalid on a daily close above", max(c["h"] for c in last5))
    else:
        inval = None

    stop = STOP_ATR * a
    stop_pct = stop / price * 100
    exposure_per_1 = 100 / stop_pct  # $ exposure per $1 of risk
    min_risk = min_exp * stop_pct / 100

    flags = []
    hits = [e for e in events if e["impact"] == "High" and e["ccy"] in ccys
            and now - timedelta(hours=1) <= e["time"] <= now + timedelta(hours=12)]
    for e in hits[:3]:
        flags.append(f"{e['ccy']} high-impact event {e['time']:%a %H:%M} UTC: {e['title']}")
    if sym in earn:
        d = earn[sym]["date"]
        days = (d - today).days
        if days <= 2:
            flags.append(f"earnings {d:%a %d %b} {earn[sym]['time']}".strip())
    if vlabel == "EXTREME":
        flags.append("volatility extreme (top 10% of its 6-month range)")
    if align == "CONFLICTING":
        flags.append("timeframes conflict")
    if today_range_atr and today_range_atr > 1.5:
        flags.append(f"already moved {today_range_atr:.1f}x ATR today (extended)")
    if stale:
        flags.append("market closed or quote stale")

    res.update(ok=True, price=price, stale=stale,
               last_bar=hourly[-1]["t"].isoformat(),
               chg_1d=pct(price, prev["c"]),
               chg_5d=pct(price, completed[-5]["c"]) if len(completed) >= 5 else None,
               chg_20d=pct(price, completed[-20]["c"]) if len(completed) >= 20 else None,
               trend={"1h": t1h, "4h": t4h, "daily": td}, alignment=align,
               vol_regime=vlabel, vol_rank=vrank,
               atr=a, atr_pct=a / price * 100, today_range_atr=today_range_atr,
               levels=levels,
               nearest_above=[(k, v, (v - price) / a) for k, v in above[:3]],
               nearest_below=[(k, v, (price - v) / a) for k, v in below[:3]],
               sessions={"asia": sess(0, 7), "london": sess(7, 13), "new_york": sess(13, 21)},
               sizing={"stop_distance": stop, "stop_pct": stop_pct,
                       "long_stop": price - stop, "short_stop": price + stop,
                       "exposure_per_1usd_risk": exposure_per_1,
                       "min_exposure": min_exp, "min_risk_at_min_exposure": min_risk,
                       "max_leverage_long": lev_l, "max_leverage_short": lev_s},
               invalidation=inval, flags=flags,
               earnings=earn.get(sym))
    res["_rets"] = {c["t"].date(): math.log(c["c"] / p["c"])
                    for p, c in zip(completed, completed[1:])}
    return res


def correlations(results, window=20):
    ok = [r for r in results if r.get("ok")]
    pairs = []
    for i, a in enumerate(ok):
        for b in ok[i + 1:]:
            common = sorted(set(a["_rets"]) & set(b["_rets"]))[-window:]
            if len(common) < 15:
                continue
            xa = [a["_rets"][d] for d in common]
            xb = [b["_rets"][d] for d in common]
            try:
                rho = statistics.correlation(xa, xb)
            except statistics.StatisticsError:
                continue
            pairs.append((a["symbol"], b["symbol"], rho))
    return pairs


def regime(results):
    by = {r["symbol"]: r for r in results if r.get("ok")}
    out = {}

    def g(s, k):
        return by[s][k] if s in by and by[s][k] is not None else None

    for k, label in (("chg_1d", "1d"), ("chg_5d", "5d")):
        parts = [(-1, "EURUSD"), (-1, "GBPUSD"), (-1, "AUDUSD"), (1, "USDJPY")]
        vals = [sgn * g(s, k) for sgn, s in parts if g(s, k) is not None]
        out[f"usd_{label}"] = sum(vals) / len(vals) if vals else None
    eq = [g(s, "chg_1d") for s in ("SPX500", "NSDQ100") if g(s, "chg_1d") is not None]
    eq1 = sum(eq) / len(eq) if eq else None
    out["equities_1d"] = eq1
    out["gold_1d"] = g("GOLD", "chg_1d")
    out["jpy_1d"] = -g("USDJPY", "chg_1d") if g("USDJPY", "chg_1d") is not None else None
    out["btc_1d"] = g("BTC", "chg_1d")
    if eq1 is None:
        tone = "unknown"
    elif eq1 <= -0.5:
        tone = "RISK-OFF"
    elif eq1 >= 0.5:
        tone = "RISK-ON"
    else:
        tone = "mixed"
    if tone == "RISK-OFF" and out["jpy_1d"] and out["jpy_1d"] > 0.3:
        tone += " (yen bid confirms)"
    out["tone"] = tone
    return out


# ---------------------------------------------------------------- output

def render(results, reg, corr, events, earn, notes, now):
    L = []
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    L.append("# Trade Intel: Market Brief")
    L.append("")
    L.append(f"**Generated:** {now:%Y-%m-%d %H:%M} UTC  ")
    L.append(f"**Next update:** about {nxt:%H:%M} UTC (hourly)  ")
    L.append("**If the generated time is more than 2 hours old, treat this brief as stale.**")
    L.append("")
    L.append("Sources: prices from the eToro Public API (same feed the account trades on); "
             "economic events from ForexFactory; earnings dates from Nasdaq. "
             "This is market analysis, not trade instructions.")
    L.append("")
    L.append("## How to read this")
    L.append("")
    L.append("- **ATR** = average true range over 14 daily bars: the typical daily move. Distances to levels are given in ATRs.")
    L.append(f"- **Suggested stop** = {STOP_ATR} x ATR from current price. A stop tighter than about 1 ATR is usually inside normal noise.")
    L.append("- **Sizing:** position exposure = (dollars you accept losing at the stop) x (exposure per $1 risk). "
             "Margin = exposure / leverage. If the exposure needed is below the instrument's minimum, that risk level is not possible on it.")
    L.append("- **Trend** uses 1h EMA20/50, 4h EMA10/20 and daily SMA20/50. ALIGNED = all three agree.")
    L.append("- **Vol regime** ranks current 20-day volatility against the last ~6 months: quiet < 25th pct, elevated > 75th, EXTREME > 90th.")
    L.append("- **Flags** are reasons professional traders typically reduce size or stand aside.")
    L.append("")

    L.append("## Market regime")
    L.append("")
    L.append(f"- **Risk tone (today):** {reg['tone']}. US indices {fpct(reg['equities_1d'])}, gold {fpct(reg['gold_1d'])}, "
             f"yen {fpct(reg['jpy_1d'])} vs USD, BTC {fpct(reg['btc_1d'])}.")
    L.append(f"- **US dollar** (vs EUR, GBP, AUD, JPY average): {fpct(reg['usd_1d'])} today, {fpct(reg['usd_5d'])} over 5 days.")
    hi = sorted([p for p in corr if abs(p[2]) >= 0.7], key=lambda p: -abs(p[2]))
    if hi:
        txt = "; ".join(f"{a}/{b} {r:+.2f}" for a, b, r in hi[:12])
        L.append(f"- **Effectively the same bet** (20-day correlation of daily returns >= 0.7): {txt}. "
                 "Holding two of these in the same direction doubles one risk; a negative figure means opposite directions double it.")
    else:
        L.append("- No instrument pairs with 20-day correlation above 0.7.")
    L.append("")

    L.append("## Event risk (next 72h, UTC)")
    L.append("")
    if events:
        L.append("| When (UTC) | Ccy | Impact | Event | Forecast | Previous |")
        L.append("|---|---|---|---|---|---|")
        for e in events:
            L.append(f"| {e['time']:%a %d %b %H:%M} | {e['ccy']} | {e['impact']} | {e['title']} | {e['forecast']} | {e['previous']} |")
    else:
        L.append("No high or medium impact events for USD, EUR, GBP, JPY or AUD in the next 72 hours.")
    L.append("")
    if earn:
        L.append("**Earnings (next 14 days):** " + "; ".join(
            f"{s} {v['date']:%a %d %b} {v['time']}".strip() for s, v in sorted(earn.items(), key=lambda kv: kv[1]["date"])))
        L.append("")
    for n in notes:
        L.append(f"_Note: {n}_")
    if notes:
        L.append("")

    ok = [r for r in results if r.get("ok")]
    flagged = [r for r in ok if r["flags"]]
    L.append("## Stand-aside list")
    L.append("")
    if flagged:
        for r in flagged:
            L.append(f"- **{r['symbol']}**: " + "; ".join(r["flags"]))
    else:
        L.append("- None flagged.")
    L.append("")

    L.append("## Overview")
    L.append("")
    L.append("| Symbol | Price | 1d | 5d | 20d | Trend 1h/4h/D | Alignment | Vol | ATR % | Flags |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in ok:
        t = r["trend"]
        L.append(f"| {r['symbol']} | {fmt(r['price'])} | {fpct(r['chg_1d'])} | {fpct(r['chg_5d'])} | {fpct(r['chg_20d'])} | "
                 f"{t['1h']}/{t['4h']}/{t['daily']} | {r['alignment']} | {r['vol_regime']} | {r['atr_pct']:.2f}% | {len(r['flags']) or ''} |")
    for r in results:
        if not r.get("ok"):
            L.append(f"| {r['symbol']} | n/a | | | | | | | | {r.get('error','')} |")
    L.append("")

    stocks = sorted([r for r in ok if r["class"] == "Stock" and r["chg_20d"] is not None], key=lambda r: -r["chg_20d"])
    if stocks:
        L.append("**Stock relative strength (20d):** " + ", ".join(f"{r['symbol']} {fpct(r['chg_20d'])}" for r in stocks))
        L.append("")

    L.append("## Instrument detail")
    for r in ok:
        p = r["price"]
        s = r["sizing"]
        lv = r["levels"]
        L.append("")
        L.append(f"### {r['symbol']} ({r['class']})")
        L.append("")
        L.append(f"- **Price** {fmt(p)} (last bar {r['last_bar'][:16].replace('T', ' ')} UTC){' - STALE' if r['stale'] else ''}. "
                 f"1d {fpct(r['chg_1d'])}, 5d {fpct(r['chg_5d'])}, 20d {fpct(r['chg_20d'])}.")
        L.append(f"- **Trend** 1h {r['trend']['1h']}, 4h {r['trend']['4h']}, daily {r['trend']['daily']}: **{r['alignment']}**. "
                 f"SMA20 {fmt(lv['sma20'], p)}, SMA50 {fmt(lv['sma50'], p)}.")
        tr = f", today's range so far {r['today_range_atr']:.1f}x ATR" if r["today_range_atr"] is not None else ""
        L.append(f"- **Volatility** {r['vol_regime']} (percentile rank {r['vol_rank']:.0f}). ATR {fmt(r['atr'], p)} ({r['atr_pct']:.2f}% of price){tr}.")
        nm = lambda k: k.replace("_", " ").replace("20d", "20-day").replace("sma", "SMA")
        ab = ", ".join(f"{nm(k)} {fmt(v, p)} (+{d:.1f} ATR)" for k, v, d in r["nearest_above"]) or "none (at highs)"
        be = ", ".join(f"{nm(k)} {fmt(v, p)} (-{d:.1f} ATR)" for k, v, d in r["nearest_below"]) or "none (at lows)"
        L.append(f"- **Levels above:** {ab}")
        L.append(f"- **Levels below:** {be}")
        sess = [f"{n.replace('_', ' ').title()} {fmt(h, p)}-{fmt(l, p)}"
                for n, (h, l) in r["sessions"].items() if h is not None]
        if sess:
            L.append(f"- **Sessions today (high-low):** " + "; ".join(sess))
        if r["invalidation"]:
            L.append(f"- **Invalidation:** {r['invalidation'][0]} {fmt(r['invalidation'][1], p)}.")
        lev = f"max leverage long {s['max_leverage_long']}x" + (
            f", short {s['max_leverage_short']}x" if s["max_leverage_short"] else ", no shorting")
        L.append(f"- **Sizing:** {STOP_ATR} ATR stop = {fmt(s['stop_distance'], p)} ({s['stop_pct']:.2f}%), "
                 f"long stop {fmt(s['long_stop'], p)}"
                 + (f", short stop {fmt(s['short_stop'], p)}. " if s["max_leverage_short"] else ". ") +
                 f"Exposure per $1 risked: ${s['exposure_per_1usd_risk']:.0f}. "
                 f"Min exposure ${s['min_exposure']:,} means minimum risk ${s['min_risk_at_min_exposure']:.2f} at this stop. {lev}.")
        if r["flags"]:
            L.append(f"- **Flags:** " + "; ".join(r["flags"]))
    L.append("")
    return "\n".join(L)


def to_json(results, reg, corr, events, earn, notes, now):
    def clean(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    rs = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    return json.dumps({
        "generated_utc": now.isoformat(), "regime": reg,
        "correlations_20d": [{"a": a, "b": b, "rho": round(r, 3)} for a, b, r in corr],
        "events": events, "earnings": earn, "notes": notes, "instruments": rs,
    }, default=clean, indent=1)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="directory of saved candle JSON ({id}_{interval}.json)")
    ap.add_argument("--now", help="override current time (ISO, UTC) for testing")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--no-web", action="store_true", help="skip calendar and earnings fetches")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now else datetime.now(timezone.utc)
    if not args.fixture and not (os.environ.get("ETORO_API_KEY") and os.environ.get("ETORO_USER_KEY")):
        sys.exit("ETORO_API_KEY and ETORO_USER_KEY must be set")

    notes = []
    if args.no_web:
        events, earn = [], {}
    else:
        events, n1 = ff_events(now)
        earn, n2 = earnings(now)
        notes += n1 + n2

    results = []
    for inst in INSTRUMENTS:
        try:
            daily = load_candles(inst[1], "OneDay", DAILY_N, args.fixture)
            hourly = load_candles(inst[1], "OneHour", HOURLY_N, args.fixture)
            results.append(analyse(inst, daily, hourly, now, events, earn))
        except Exception as e:
            results.append({"symbol": inst[0], "class": inst[2], "ok": False,
                            "error": f"data error: {type(e).__name__}"})
        if not args.fixture:
            time.sleep(0.6)

    if not any(r.get("ok") for r in results):
        sys.exit("No instrument data retrieved; not overwriting the previous brief.")

    reg = regime(results)
    corr = correlations(results)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "BRIEF.md"), "w") as f:
        f.write(render(results, reg, corr, events, earn, notes, now))
    with open(os.path.join(args.out, "brief.json"), "w") as f:
        f.write(to_json(results, reg, corr, events, earn, notes, now))
    bad = [r["symbol"] for r in results if not r.get("ok")]
    print(f"Brief written for {len(results) - len(bad)} instruments" + (f"; failed: {', '.join(bad)}" if bad else ""))


if __name__ == "__main__":
    main()
