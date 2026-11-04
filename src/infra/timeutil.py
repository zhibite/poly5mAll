"""Window / clock helpers.

All strategy timestamps are UTC epoch seconds.  5m Polymarket windows start
on unix-second multiples of 300, so ``floor(now / 300) * 300`` is the slug
epoch used by ``{asset}-updown-5m-{epoch}``.
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
