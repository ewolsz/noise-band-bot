"""The noise band rule, in one place. Used by the bot and checked against the backtest.

All inputs are plain lists so this file needs nothing but Python.
A session is 390 one-minute bars, index 0 = the 9:30 bar. Each bar is (o, h, l, c, v).
"""
from __future__ import annotations

N = 390


def minute_index(hhmm: str) -> int:
    """Index of the bar that ENDS at hh:mm (10:00 -> 29)."""
    h, m = map(int, hhmm.split(":"))
    return (h - 9) * 60 + m - 30 - 1


def check_indexes(cfg: dict) -> list[int]:
    return list(range(minute_index(cfg["first_check"]), minute_index(cfg["last_check"]) + 1,
                      cfg["check_every_minutes"]))


def fill_session(bars_by_index: dict, upto: int):
    """{index: (o,h,l,c,v)} -> list of bars 0..upto with gaps carried forward.
    Returns None if the session has no bar in its first 5 minutes."""
    first = min((i for i in bars_by_index if i >= 0), default=None)
    if first is None or first > 4:
        return None
    out, last = [], bars_by_index[first][0]
    for i in range(upto + 1):
        b = bars_by_index.get(i)
        if b is None:
            b = (last, last, last, last, 0.0)
        out.append(b)
        last = b[3]
    return out


def sigma_from_history(sessions: list, lookback: int):
    """Average |close/open - 1| at each minute over the last `lookback` full sessions."""
    if len(sessions) < lookback:
        return None
    recent = sessions[-lookback:]
    moves = [[abs(b[3] / s[0][0] - 1.0) for b in s] for s in recent]
    return [sum(m[i] for m in moves) / lookback for i in range(N)]


def signal_at(today: list, sigma: list, cfg: dict, i: int):
    """Signal on the close of bar i. Returns (direction, band_distance, detail).
    direction: +1 buy calls / -1 buy puts / 0 nothing."""
    op = today[0][0]
    rmin = cfg["range_minutes"]
    rh = max(b[1] for b in today[:rmin])
    rl = min(b[2] for b in today[:rmin])
    room = cfg["room_fraction_of_range"] * (rh - rl)
    pv = vv = 0.0
    for b in today[: i + 1]:
        pv += (b[1] + b[2] + b[3]) / 3.0 * b[4]
        vv += b[4]
    c = today[i][3]
    vwap = pv / vv if vv else c
    dist = op * cfg["band_multiplier"] * sigma[i]
    detail = {"close": c, "open": op, "range_high": rh, "range_low": rl, "room": room,
              "band_low": op - dist, "band_high": op + dist, "vwap": vwap}
    if c < rl - room and c < op - dist and c < vwap:
        return -1, dist, detail
    if c > rh + room and c > op + dist and c > vwap:
        return 1, dist, detail
    return 0, dist, detail


def trail(closes_since_entry: list, entry: float, direction: int, arm: float, giveback: float):
    """Has the trailing exit fired? Returns (fired, best_gain, gain_now)."""
    best, armed = 0.0, False
    fav = 0.0
    for c in closes_since_entry:
        fav = direction * (c - entry)
        if fav > best:
            best = fav
        if best >= arm:
            armed = True
        if armed and fav <= (1.0 - giveback) * best:
            return True, best, fav
    return False, best, fav


def pick_contract(contracts: list, price: float):
    """Nearest tradable strike to the price. contracts: dicts with symbol, strike_price, tradable."""
    ok = [c for c in contracts if c.get("tradable", True)]
    if not ok:
        return None
    return min(ok, key=lambda c: (abs(float(c["strike_price"]) - price), float(c["strike_price"])))


def size(equity: float, ask: float, opt: dict) -> int:
    """How many contracts. 0 means skip."""
    if ask <= 0:
        return 0
    cost = ask * 100.0
    n = int(equity * opt["budget_pct_of_equity"] // cost)
    if n == 0 and cost <= equity * opt["one_contract_allowed_up_to_pct"]:
        n = 1
    return min(n, opt["max_contracts"])
