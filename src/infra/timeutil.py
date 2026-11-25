"""Window / clock helpers.

All strategy timestamps are UTC epoch seconds.  Polygon Up/Down windows
start on unix-second multiples of their length: 300 for 5m
(``{asset}-updown-5m-{epoch}``) and 900 for 15m
(``{asset}-updown-15m-{epoch}``).
"""
from __future__ import annotations

import datetime as dt


def window_start(now: float, window_sec: int = 300) -> int:
    return int(now // window_sec) * window_sec


def window_end(start_ts: int, window_sec: int = 300) -> int:
    return int(start_ts) + int(window_sec)


def utc_iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def utc_hms(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S")


def day_key(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
