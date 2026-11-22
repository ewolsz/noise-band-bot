#!/usr/bin/env python3
"""Noise band bot: the rule from the 'Noise Band' video, run hands-off on an
Alpaca PAPER account. One QQQ option trade a day at most.

    python3 bot/noise_band_bot.py status            # account, open position, today's order
    python3 bot/noise_band_bot.py run               # the day's session (waits, checks, trades, exits)
    python3 bot/noise_band_bot.py run --dry-run     # same, but places no orders
    python3 bot/noise_band_bot.py look              # one look right now, no orders
    python3 bot/noise_band_bot.py flatten           # backup: sell if it is past 3:45 PM (add --now to sell at once)
    python3 bot/noise_band_bot.py report            # results so far -> report/

The day:
  1. 9:30 to 9:45: the opening range (high and low).
  2. Noise band: the average move from the open at each minute over the last 20
     full sessions, plus 25%, drawn above and below today's open.
  3. 10:00 to 2:00, every 10 minutes: if QQQ's close is past the opening range
     (with a little room), past the band, and on the same side of VWAP, buy puts
     (down) or calls (up): nearest strike, expiring the next trading day.
  4. Hold. Once QQQ has run by the band distance, sell when it gives back half of
     its best gain. Otherwise sell at 3:45 PM. One trade a day.

Safety:
  - Paper only. The trading URL is Alpaca's paper endpoint; there is no setting
    that changes it.
  - Off switch: while a file called STOP sits in the top folder, no orders are sent.
  - It only ever buys options (loss limited to what was paid) and sells what it bought.
  - Alpaca is its memory: orders are tagged nb-YYYYMMDD-..., so any run knows what
    was done today and can pick a position back up.

Keys: environment variables ALPACA_KEY and ALPACA_SECRET (this bot's OWN paper
account). Phone notes: NTFY_TOPIC (optional).

The rule failed its backtest on QQQ shares (2016-2020). The option version has
never been backtested. This is a forward paper check. Not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import noise_rules as R  # noqa: E402

ET = ZoneInfo("America/New_York")
TRADING_URL = "https://paper-api.alpaca.markets"     # paper. Do not change casually.
DATA_URL = "https://data.alpaca.markets"
CONFIG = ROOT / "config" / "noise_band.json"
STOP_FILE = ROOT / "STOP"
TAG = "nb"
SETTLE = 20            # seconds to wait after a minute closes before reading it
SIP_DELAY = 16         # minutes: the free plan cannot read full-market bars newer than this


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_cfg(path: Path = CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text())
    if cfg.get("paper") is not True:
        sys.exit("config: this bot is paper-only")
    return cfg


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        tz_at = max(rest.find("+"), rest.find("-"))
        frac, tz = (rest[:tz_at], rest[tz_at:]) if tz_at >= 0 else (rest, "")
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).astimezone(ET)


def notify(title: str, body: str = "") -> None:
    print(f"[note] {title}" + (f"\n{body}" if body else ""), flush=True)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=(body or title).encode(),
                                     headers={"Title": title.encode("ascii", "replace").decode()},
                                     method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:                           # a note must never stop the bot
        print(f"[note not sent: {e}]", flush=True)


def summary(md: str) -> None:
    print(md, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def make_tag(day: str, direction: int, entry_u: float, dist: float) -> str:
    """nb-20261123-P-u63203-d412 : day, put/call, QQQ price in cents, band distance in cents."""
    return (f"{TAG}-{day.replace('-', '')}-{'C' if direction > 0 else 'P'}"
            f"-u{int(round(entry_u * 100))}-d{int(round(dist * 100))}")


def parse_tag(cid: str):
    p = (cid or "").split("-")
    if len(p) != 5 or p[0] != TAG or len(p[1]) != 8 or p[2] not in "CP" \
            or not p[3].startswith("u") or not p[4].startswith("d"):
        return None
    try:
        d = p[1]
        return {"day": f"{d[:4]}-{d[4:6]}-{d[6:]}", "dir": 1 if p[2] == "C" else -1,
                "entry_u": int(p[3][1:]) / 100.0, "dist": int(p[4][1:]) / 100.0}
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

class Alpaca:
    def __init__(self, key: str | None = None, secret: str | None = None):
        self.key = key or os.environ.get("ALPACA_KEY", "")
        self.secret = secret or os.environ.get("ALPACA_SECRET", "")
        if not self.key or not self.secret:
            sys.exit("ALPACA_KEY / ALPACA_SECRET are not set")

    def _call(self, method, base, path, params=None, body=None, tries=3):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
                "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                    _time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f"Alpaca {method} {path} -> {e.code}: {text}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt + 1 < tries:
                    _time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f"Alpaca {method} {path} -> {e}") from None

    # trading
    def account(self):            return self._call("GET", TRADING_URL, "/v2/account")
    def clock(self):              return self._call("GET", TRADING_URL, "/v2/clock")
    def positions(self):          return self._call("GET", TRADING_URL, "/v2/positions")
    def order(self, oid):         return self._call("GET", TRADING_URL, f"/v2/orders/{oid}")

    def calendar(self, start, end):
        return self._call("GET", TRADING_URL, "/v2/calendar", {"start": start, "end": end})

    def orders(self, after_iso):
        return self._call("GET", TRADING_URL, "/v2/orders",
                          {"status": "all", "after": after_iso, "limit": 500, "direction": "asc"})

    def contracts(self, underlying, expiry, kind, lo, hi):
        r = self._call("GET", TRADING_URL, "/v2/options/contracts", {
            "underlying_symbols": underlying, "expiration_date": expiry, "type": kind,
            "strike_price_gte": f"{lo:.2f}", "strike_price_lte": f"{hi:.2f}",
            "status": "active", "limit": 100})
        return r.get("option_contracts", [])

    def submit(self, symbol, qty, side, cid=None):
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market",
                "time_in_force": "day"}
        if cid:
            body["client_order_id"] = cid
        return self._call("POST", TRADING_URL, "/v2/orders", body=body, tries=1)

    # data
    def bars(self, symbol, start: datetime, end: datetime, feed: str):
        out, token = [], None
        while True:
            p = {"timeframe": "1Min", "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "feed": feed, "limit": 10000, "adjustment": "raw"}
            if token:
                p["page_token"] = token
            r = self._call("GET", DATA_URL, f"/v2/stocks/{symbol}/bars", p)
            out += r.get("bars") or []
            token = r.get("next_page_token")
            if not token:
                return out

    def option_quote(self, symbol):
        r = self._call("GET", DATA_URL, "/v1beta1/options/quotes/latest",
                       {"symbols": symbol, "feed": "indicative"})
        q = (r.get("quotes") or {}).get(symbol) or {}
        return float(q.get("bp") or 0), float(q.get("ap") or 0)


# ---------------------------------------------------------------------------
# market data -> sessions
# ---------------------------------------------------------------------------

def index_bars(raw: list) -> dict:
    """Alpaca bars -> {date: {minute index: (o,h,l,c,v)}} for the regular session."""
    days: dict = {}
    for b in raw:
        t = parse_ts(b["t"])
        i = (t.hour - 9) * 60 + t.minute - 30
        if 0 <= i < R.N:
            days.setdefault(t.strftime("%Y-%m-%d"), {})[i] = (
                float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0))
    return days


def load_history(api, cfg, today: str, now: datetime):
    """Full sessions before today (full-market data), oldest first."""
    start = (now - timedelta(days=50)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = index_bars(api.bars(cfg["symbol"], start, end, "sip"))
    full = []
    for d in sorted(days):
        if d >= today or len(days[d]) < 380:          # half days and broken days are left out
            continue
        s = R.fill_session(days[d], R.N - 1)
        if s:
            full.append(s)
    return full


def load_today(api, cfg, now: datetime, upto: int):
    """Today's bars 0..upto: full-market data where the free plan allows, IEX for the newest minutes."""
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    cut = now - timedelta(minutes=SIP_DELAY)
    merged: dict = {}
    if cut > open_t:
        merged.update(index_bars(api.bars(cfg["symbol"], open_t, cut, "sip")).get(now.strftime("%Y-%m-%d"), {}))
    recent = index_bars(api.bars(cfg["symbol"], max(open_t, cut - timedelta(minutes=1)), now, "iex"))
    for i, b in recent.get(now.strftime("%Y-%m-%d"), {}).items():
        merged.setdefault(i, b)
    return R.fill_session(merged, upto) if merged else None


# ---------------------------------------------------------------------------
# today's state, from Alpaca
# ---------------------------------------------------------------------------

def todays_orders(api, today: str):
    after = datetime.fromisoformat(today).replace(tzinfo=ET).astimezone(timezone.utc)
    mine = []
    for o in api.orders(after.strftime("%Y-%m-%dT%H:%M:%SZ")):
        tag = parse_tag(o.get("client_order_id"))
        if tag and tag["day"] == today:
            mine.append((o, tag))
    return mine


def open_option(api, symbol_root: str):
    for p in api.positions():
        if p.get("asset_class") == "us_option" and p["symbol"].startswith(symbol_root):
            return p
    return None


def wait_fill(api, oid, seconds=60):
    for _ in range(seconds // 3):
        o = api.order(oid)
        if o["status"] == "filled":
            return o
        if o["status"] in ("canceled", "rejected", "expired"):
            return o
        _time.sleep(3)
    return api.order(oid)


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

def sleep_until(t: datetime):
    while True:
        s = (t - now_et()).total_seconds()
        if s <= 0:
            return
        _time.sleep(min(s, 30))


def slot_time(now: datetime, idx: int) -> datetime:
    """Clock time at which bar idx has closed and settled."""
    return now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=idx + 1, seconds=SETTLE)


def next_trading_day(api, today: str) -> str:
    end = (datetime.fromisoformat(today) + timedelta(days=10)).strftime("%Y-%m-%d")
    days = [c["date"] for c in api.calendar(today, end) if c["date"] > today]
    return days[0]


def enter(api, cfg, today, direction, dist, price, dry):
    opt = cfg["option"]
    kind = "call" if direction > 0 else "put"
    expiry = next_trading_day(api, today)
    cons = api.contracts(cfg["symbol"], expiry, kind, price - 5, price + 5)
    con = R.pick_contract(cons, price)
    if not con:
        notify("Noise band: signal, no trade", f"No {kind} contracts found for {expiry} near {price:.2f}.")
        return None
    bid, ask = api.option_quote(con["symbol"])
    mid = (bid + ask) / 2 if bid and ask else 0
    if not ask or (mid and (ask - bid) / mid > opt["max_spread_pct"]):
        notify("Noise band: signal, no trade", f"{con['symbol']}: quote missing or too wide (bid {bid}, ask {ask}).")
        return None
    equity = float(api.account()["equity"])
    qty = R.size(equity, ask, opt)
    if qty < 1:
        notify("Noise band: signal, no trade",
               f"One {con['symbol']} costs ${ask*100:,.0f}, over the limit for a ${equity:,.0f} account.")
        return None
    tag = make_tag(today, direction, price, dist)
    line = (f"{'CALLS' if direction > 0 else 'PUTS'}: {qty} x {con['symbol']} "
            f"(strike {float(con['strike_price']):g}, expires {expiry}) at about ${ask:.2f}. QQQ {price:.2f}.")
    if dry:
        summary(f"DRY RUN, no order sent. Would buy {line}")
        return {"dry": True, "symbol": con["symbol"], "tag": parse_tag(tag)}
    o = api.submit(con["symbol"], qty, "buy", tag)
    o = wait_fill(api, o["id"])
    if o["status"] != "filled":
        notify("Noise band: buy order not filled", f"{con['symbol']} status {o['status']}. Check the account.")
        return None
    notify("Noise band: bought", line.replace(f"about ${ask:.2f}", f"${float(o['filled_avg_price']):.2f}")
           + " Paper account.")
    return {"dry": False, "symbol": con["symbol"], "tag": parse_tag(tag),
            "filled_at": parse_ts(o["filled_at"])}


def manage(api, cfg, today, pos_symbol, tag, since: datetime, dry):
    """Watch QQQ each minute; sell on the give-back rule or at the flat time."""
    flat_idx = R.minute_index(cfg["flat_at"])
    start_idx = max(0, (since.hour - 9) * 60 + since.minute - 30)
    while True:
        now = now_et()
        idx = (now.hour - 9) * 60 + now.minute - 30 - 1           # last closed bar
        reason = None
        if idx >= flat_idx:
            reason = f"{cfg['flat_at']} exit"
        else:
            bars = load_today(api, cfg, now, idx)
            if bars:
                closes = [b[3] for b in bars[start_idx:idx + 1]]
                fired, best, fav = R.trail(closes, tag["entry_u"], tag["dir"], tag["dist"], cfg["trail_giveback"])
                if fired:
                    reason = f"gave back half (best QQQ gain {best:.2f}, now {fav:.2f})"
        if reason:
            if dry:
                summary(f"DRY RUN: would sell {pos_symbol}: {reason}")
                return
            return sell(api, pos_symbol, reason)
        nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=SETTLE)
        sleep_until(nxt)


def sell(api, symbol_root_or_symbol, reason):
    pos = open_option(api, symbol_root_or_symbol[:3])
    if not pos:
        print("nothing to sell")
        return
    qty = abs(int(float(pos["qty"])))
    o = wait_fill(api, api.submit(pos["symbol"], qty, "sell")["id"])
    if o["status"] != "filled":
        notify("Noise band: SELL NOT FILLED", f"{pos['symbol']} status {o['status']}. Check the paper account.")
        return
    paid, got = float(pos["avg_entry_price"]), float(o["filled_avg_price"])
    pnl = (got - paid) * 100 * qty
    notify(f"Noise band: sold, {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}",
           f"{qty} x {pos['symbol']}: paid ${paid:.2f}, sold ${got:.2f} ({(got/paid-1)*100:+.0f}%). {reason}. Paper account.")


def run(api, cfg, dry: bool):
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    if STOP_FILE.exists() and not dry:
        summary("STOP file present: the bot is switched off. No orders.")
        return
    cal = api.calendar(today, today)
    if not cal or cal[0]["date"] != today:
        summary("Market closed today. Nothing to do.")
        return
    if cal[0]["close"] not in ("16:00", "1600"):
        summary("Early-close day. The bot sits out.")
        return
    if now.hour < 9:
        summary("Too early (wrong-season schedule slot). Exiting.")
        return

    checks = R.check_indexes(cfg)
    mine = todays_orders(api, today)
    pos = open_option(api, cfg["symbol"])
    if pos and mine:                                      # picked back up after a restart
        o, tag = mine[0]
        since = parse_ts(o["filled_at"]) if o.get("filled_at") else now
        summary(f"Open position {pos['symbol']} found; watching it.")
        return manage(api, cfg, today, pos["symbol"], tag, since, dry)
    if mine:
        summary("Today's trade is already done. One trade a day.")
        return
    if pos:
        notify("Noise band: unknown option position", f"{pos['symbol']} is open but was not opened today by this bot. Not touching it.")
        return

    history = load_history(api, cfg, today, now)
    sigma = R.sigma_from_history(history, cfg["lookback_sessions"])
    if sigma is None:
        notify("Noise band: not enough history", f"Only {len(history)} full sessions found; need {cfg['lookback_sessions']}.")
        return
    summary(f"{today}: history loaded ({len(history)} sessions). Checking every "
            f"{cfg['check_every_minutes']} minutes from {cfg['first_check']} to {cfg['last_check']}.")

    for idx in checks:
        t = slot_time(now_et(), idx)
        if (now_et() - t).total_seconds() > 240:          # this check time has passed (late start)
            continue
        sleep_until(t)
        bars = load_today(api, cfg, now_et(), idx)
        if not bars:
            print(f"check {idx}: no bars yet", flush=True)
            continue
        direction, dist, d = R.signal_at(bars, sigma, cfg, idx)
        print(f"{now_et():%H:%M} QQQ {d['close']:.2f} | range {d['range_low']:.2f}-{d['range_high']:.2f} "
              f"room {d['room']:.2f} | band {d['band_low']:.2f}-{d['band_high']:.2f} | VWAP {d['vwap']:.2f} "
              f"-> {'CALLS' if direction > 0 else 'PUTS' if direction < 0 else 'nothing'}", flush=True)
        if direction:
            got = enter(api, cfg, today, direction, dist, d["close"], dry)
            if got:
                return manage(api, cfg, today, got["symbol"], got["tag"], now_et(), dry)
            return                                         # a signal that could not be traded still uses the day
    summary("No signal today. No trade.")


def look(api, cfg):
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    idx = min((now.hour - 9) * 60 + now.minute - 30 - 1, R.N - 1)
    history = load_history(api, cfg, today, now)
    sigma = R.sigma_from_history(history, cfg["lookback_sessions"])
    summary(f"History: {len(history)} full sessions.")
    if sigma is None or idx < cfg["range_minutes"]:
        summary("Not enough data for a look right now (market closed, or before 9:45).")
        return
    bars = load_today(api, cfg, now, idx)
    if not bars:
        summary("No bars for today (market closed?).")
        return
    direction, dist, d = R.signal_at(bars, sigma, cfg, idx)
    summary(f"QQQ {d['close']:.2f}. Opening range {d['range_low']:.2f} to {d['range_high']:.2f}. "
            f"Band {d['band_low']:.2f} to {d['band_high']:.2f}. VWAP {d['vwap']:.2f}. "
            f"Signal now: {'CALLS' if direction > 0 else 'PUTS' if direction < 0 else 'none'}. No orders sent.")


def status(api, cfg):
    a = api.account()
    summary(f"Paper account equity ${float(a['equity']):,.2f}, options level {a.get('options_trading_level', '?')}, "
            f"options buying power ${float(a.get('options_buying_power') or 0):,.2f}.")
    pos = api.positions()
    summary("Positions: " + (", ".join(f"{p['qty']} {p['symbol']}" for p in pos) or "none"))
    summary("Switched " + ("OFF (STOP file present)" if STOP_FILE.exists() else "ON"))


def report(api, cfg, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    orders = api.orders("2026-11-01T00:00:00Z")
    by_symbol_day, rows = {}, []
    for o in orders:
        if o.get("asset_class") != "us_option" or o["status"] != "filled":
            continue
        day = parse_ts(o["filled_at"]).strftime("%Y-%m-%d")
        by_symbol_day.setdefault((day, o["symbol"]), []).append(o)
    for (day, sym), os_ in sorted(by_symbol_day.items()):
        buys = [o for o in os_ if o["side"] == "buy" and parse_tag(o.get("client_order_id"))]
        sells = [o for o in os_ if o["side"] == "sell"]
        if not buys or not sells:
            continue
        qty = int(float(buys[0]["filled_qty"]))
        paid, got = float(buys[0]["filled_avg_price"]), float(sells[0]["filled_avg_price"])
        rows.append({"day": day, "contract": sym, "contracts": qty, "paid": paid, "sold": got,
                     "pnl_usd": round((got - paid) * 100 * qty, 2), "pct": round((got / paid - 1) * 100, 1)})
    with (out / "trades.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["day", "contract", "contracts", "paid", "sold", "pnl_usd", "pct"])
        w.writeheader()
        w.writerows(rows)
    n = len(rows)
    wins = sum(r["pnl_usd"] > 0 for r in rows)
    total = sum(r["pnl_usd"] for r in rows)
    md = [f"## Noise band bot: {n} closed trades", ""]
    if n:
        md += [f"- Total: ${total:+,.2f}", f"- Winners: {wins} of {n}",
               f"- Average trade: ${total / n:+,.2f} ({sum(r['pct'] for r in rows) / n:+.1f}% of the price paid)",
               "", "| day | contract | n | paid | sold | result |", "|---|---|---|---|---|---|"]
        md += [f"| {r['day']} | {r['contract']} | {r['contracts']} | {r['paid']:.2f} | {r['sold']:.2f} | ${r['pnl_usd']:+,.2f} |"
               for r in rows]
    md += ["", "Judge after about 100 trades, not before. Fewer than that is mostly noise."]
    (out / "report.md").write_text("\n".join(md) + "\n")
    summary("\n".join(md))


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--dry-run", action="store_true")
    sub.add_parser("look"); sub.add_parser("status")
    fl = sub.add_parser("flatten"); fl.add_argument("--now", action="store_true")
    rp = sub.add_parser("report"); rp.add_argument("--out", default="report")
    a = p.parse_args(argv)
    cfg, api = load_cfg(), Alpaca()
    try:
        if a.cmd == "run":
            run(api, cfg, a.dry_run)
        elif a.cmd == "look":
            look(api, cfg)
        elif a.cmd == "status":
            status(api, cfg)
        elif a.cmd == "flatten":
            t = now_et()
            flat = R.minute_index(cfg["flat_at"]) + 1
            if a.now or (t.hour - 9) * 60 + t.minute - 30 >= flat:
                sell(api, cfg["symbol"], "flatten")
            else:
                summary(f"Before {cfg['flat_at']} New York time: leaving any position alone.")
        else:
            report(api, cfg, Path(a.out))
    except Exception as e:
        notify("Noise band bot: error", str(e)[:300])
        raise


if __name__ == "__main__":
    main()
