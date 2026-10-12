"""Download and check public Binance USD-M prices and funding rates."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen
import zipfile

import numpy as np
import pandas as pd


BASE = "https://data.binance.vision/data/futures/um/monthly"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
KINDS = ("klines", "markPriceKlines", "fundingRate")


def month_range(start, end):
    """The end month is included."""
    if not all(re.fullmatch(r"\d{4}-\d{2}", x) for x in (start, end)):
        raise ValueError("Use months like 2021-01 and 2025-12")
    months = pd.period_range(start, end, freq="M")
    if len(months) == 0:
        raise ValueError("The start month must be before the end month")
    return [str(x) for x in months]


def archive_name(kind, symbol, month):
    if kind == "fundingRate":
        return f"{kind}/{symbol}/{symbol}-fundingRate-{month}.zip"
    return f"{kind}/{symbol}/1h/{symbol}-1h-{month}.zip"


def read_url(url):
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "pair-trade-lab public research"})
            with urlopen(request, timeout=45) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def save_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_bytes(content)
    temp.replace(path)


def fetch_archive(job):
    cache_dir, kind, symbol, month, refresh = job
    name = archive_name(kind, symbol, month)
    frequency = "daily" if len(month) == 10 else "monthly"
    base = BASE.replace("/monthly", f"/{frequency}")
    url = f"{base}/{name}"
    path = cache_dir / "archives" / name
    checksum_path = path.with_name(path.name + ".CHECKSUM")
    if refresh or not checksum_path.exists():
        checksum = read_url(url + ".CHECKSUM")
    else:
        checksum = checksum_path.read_bytes()
    parts = checksum.decode("ascii").strip().split()
    if len(parts) != 2 or not re.fullmatch("[0-9a-fA-F]{64}", parts[0]):
        raise ValueError(f"Bad checksum file: {url}")
    if parts[1].lstrip("*") != path.name:
        raise ValueError(f"Checksum names a different file: {url}")
    expected = parts[0].lower()
    raw = path.read_bytes() if path.exists() else b""
    if hashlib.sha256(raw).hexdigest() != expected:
        raw = read_url(url)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f"SHA256 failed: {url}")
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != actual:
        save_bytes(path, raw)
    save_bytes(checksum_path, checksum)
    return {
        "kind": kind, "symbol": symbol, "period": month, "frequency": frequency,
        "url": url, "checksum_url": url + ".CHECKSUM",
        "sha256": actual, "bytes": len(raw), "checksum_verified": True,
    }


def read_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1 or not names[0].endswith(".csv"):
            raise ValueError(f"Expected one CSV in {path.name}")
        with archive.open(names[0]) as source:
            frame = pd.read_csv(source, header=None, dtype=str)
    # Early price files have no header; later files and funding files do.
    if pd.isna(pd.to_numeric(frame.iloc[0, 0], errors="coerce")):
        frame = frame.iloc[1:].reset_index(drop=True)
    frame = frame.apply(pd.to_numeric, errors="raise")
    if frame.empty or not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise ValueError(f"Empty or non-finite data in {path.name}")
    return frame


def utc_time(values):
    """Detect units rather than applying the spot-data 2025 rule to futures."""
    smallest = int(values.min())
    largest = int(values.max())
    for unit, low, high in (("ms", 10**12, 10**14), ("us", 10**15, 10**17)):
        if low <= smallest <= largest < high:
            return pd.DatetimeIndex(pd.to_datetime(values, unit=unit, utc=True)), unit
    raise ValueError("Unknown or mixed timestamp units")


def read_prices(cache_dir, kind, symbol, months):
    frames, units = [], set()
    for month in months:
        raw = read_archive(cache_dir / "archives" / archive_name(kind, symbol, month))
        if raw.shape[1] != 12:
            raise ValueError(f"Expected 12 price columns: {kind} {symbol} {month}")
        index, unit = utc_time(raw[0])
        units.add(unit)
        ticks_per_hour = 3_600_000 if unit == "ms" else 3_600_000_000
        if not ((raw[6] - raw[0]) == ticks_per_hour - 1).all():
            raise ValueError(f"Wrong candle close times: {kind} {symbol} {month}")
        if not (index == index.floor("h")).all():
            raise ValueError(f"Price bars are not hourly: {kind} {symbol} {month}")
        prices = raw[[1, 2, 3, 4]].to_numpy(dtype=float)
        if (prices <= 0).any() or (prices[:, 2] > prices.min(axis=1)).any() or (prices[:, 1] < prices.max(axis=1)).any():
            raise ValueError(f"Invalid OHLC prices: {kind} {symbol} {month}")
        frame = pd.DataFrame({"open": prices[:, 0], "close": prices[:, 3]}, index=index)
        frames.append(frame)
    frame = pd.concat(frames)
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(f"Duplicate or unsorted prices: {kind} {symbol}")
    return frame, sorted(units)


def read_funding(cache_dir, symbol, months):
    frames, units = [], set()
    for month in months:
        raw = read_archive(cache_dir / "archives" / archive_name("fundingRate", symbol, month))
        if raw.shape[1] != 3:
            raise ValueError(f"Expected 3 funding columns: {symbol} {month}")
        index, unit = utc_time(raw[0])
        units.add(unit)
        frame = pd.DataFrame({
            "symbol": symbol,
            "funding_rate": raw[2].to_numpy(dtype=float),
            "funding_interval_hours": raw[1].to_numpy(dtype=float),
        }, index=index)
        frames.append(frame)
    frame = pd.concat(frames)
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError(f"Duplicate or unsorted funding events: {symbol}")
    if (frame.funding_interval_hours <= 0).any():
        raise ValueError(f"Invalid funding intervals: {symbol}")
    return frame, sorted(units)


def build_cache(cache_dir="cache", start="2021-01", end="2025-12", refresh=False):
    """Return prices, funding events, checks and source manifest; write local CSVs."""
    cache_dir = Path(cache_dir)
    months = month_range(start, end)
    start_time = pd.Timestamp(months[0], tz="UTC")
    stop_time = pd.Timestamp(str(pd.Period(months[-1], freq="M") + 1), tz="UTC")
    expected = pd.date_range(start_time, stop_time, freq="h", inclusive="left")
    jobs = [(cache_dir, kind, symbol, month, refresh)
            for kind in KINDS for symbol in SYMBOLS for month in months]
    print(f"Checking {len(jobs)} public archives...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        manifest = list(pool.map(fetch_archive, jobs))
    joined = pd.DataFrame(index=expected)
    joined.index.name = "timestamp"
    funding_frames = []
    checks = {
        "start_month": start, "end_month": end, "timezone": "UTC",
        "expected_hours": len(expected), "archive_count": len(manifest),
        "all_archive_sha256_verified": True,
        "funding_hour_window": "[t, t + 1 hour)",
        "funding_price": "mark-price open of the event hour; settlement-price proxy",
        "symbols": {},
    }
    for symbol in SYMBOLS:
        short = symbol.removesuffix("USDT")
        prices, price_units = read_prices(cache_dir, "klines", symbol, months)
        marks, mark_units = read_prices(cache_dir, "markPriceKlines", symbol, months)
        funding, funding_units = read_funding(cache_dir, symbol, months)
        # Some monthly mark files omit whole days that exist in the daily archive.
        mark_gaps = expected.difference(marks.index)
        gap_days = sorted(set(mark_gaps.strftime("%Y-%m-%d")))
        if gap_days:
            print(f"Checking {len(gap_days)} daily mark archives for {symbol} gaps...", flush=True)
            jobs = [(cache_dir, "markPriceKlines", symbol, day, refresh) for day in gap_days]
            with ThreadPoolExecutor(max_workers=6) as pool:
                manifest.extend(pool.map(fetch_archive, jobs))
            daily_marks, daily_units = read_prices(cache_dir, "markPriceKlines", symbol, gap_days)
            overlap = daily_marks.index.intersection(marks.index)
            if not daily_marks.loc[overlap].equals(marks.loc[overlap]):
                raise ValueError(f"Daily and monthly mark prices disagree: {symbol}")
            marks = pd.concat([marks, daily_marks.loc[daily_marks.index.intersection(mark_gaps)]]).sort_index()
            mark_units = sorted(set(mark_units + daily_units))
        for label, frame in (("trade", prices), ("mark", marks)):
            missing = expected.difference(frame.index)
            extra = frame.index.difference(expected)
            if len(missing) or len(extra):
                raise ValueError(f"{symbol} {label}: {len(missing)} missing and {len(extra)} extra hours")
        if not ((funding.index >= start_time) & (funding.index < stop_time)).all():
            raise ValueError(f"Funding outside requested months: {symbol}")
        event_hours = funding.index.floor("h")
        if event_hours.has_duplicates:
            raise ValueError(f"More than one funding event per hour: {symbol}")
        # This research sample uses 8-hour funding. Do not silently fill missing events.
        expected_funding = pd.date_range(start_time, stop_time, freq="8h", inclusive="left")
        if not (funding.funding_interval_hours == 8).all() or not event_hours.equals(expected_funding):
            raise ValueError(f"Funding schedule changed or has missing events: {symbol}")
        offsets = (funding.index - event_hours).total_seconds()
        funding["mark_open_proxy"] = marks.open.reindex(event_hours).to_numpy()
        funding.index.name = "timestamp"
        funding_frames.append(funding)
        joined[f"{short}_open"] = prices.open
        joined[f"{short}_close"] = prices.close
        joined[f"{short}_mark_open"] = marks.open
        hourly_rates = funding.funding_rate.groupby(event_hours).sum()
        joined[f"{short}_funding"] = hourly_rates.reindex(expected, fill_value=0.0)
        checks["symbols"][symbol] = {
            "price_rows": len(prices), "mark_price_rows": len(marks),
            "funding_events": len(funding), "missing_price_hours": 0,
            "missing_mark_price_hours": 0, "duplicate_price_times": 0,
            "monthly_mark_price_missing_hours": len(mark_gaps),
            "daily_mark_supplement_days": gap_days,
            "daily_mark_supplement_hours": len(mark_gaps),
            "duplicate_funding_times": 0, "missing_funding_windows": 0,
            "funding_interval_hours": [8],
            "funding_offset_seconds_min": float(offsets.min()),
            "funding_offset_seconds_max": float(offsets.max()),
            "price_time_units": price_units, "mark_time_units": mark_units,
            "funding_time_units": funding_units,
            "first_price_time": prices.index[0].isoformat(),
            "last_price_time": prices.index[-1].isoformat(),
            "first_funding_time": funding.index[0].isoformat(),
            "last_funding_time": funding.index[-1].isoformat(),
        }
    events = pd.concat(funding_frames).sort_index(kind="stable")
    checks["archive_count"] = len(manifest)
    if not np.isfinite(joined.to_numpy()).all():
        raise ValueError("Joined data has missing or non-finite values")
    cache_dir.mkdir(parents=True, exist_ok=True)
    joined.to_csv(cache_dir / "joined.csv")
    events.to_csv(cache_dir / "funding.csv")
    checks["joined_csv_sha256"] = hashlib.sha256((cache_dir / "joined.csv").read_bytes()).hexdigest()
    checks["funding_csv_sha256"] = hashlib.sha256((cache_dir / "funding.csv").read_bytes()).hexdigest()
    (cache_dir / "data-check.json").write_text(json.dumps(checks, indent=2) + "\n")
    return joined, events, checks, manifest


def load_data(cache_dir="cache", start="2021-01", end="2025-12"):
    """UTC hourly open, close, mark-open and funding-rate columns for BTC and ETH."""
    return build_cache(cache_dir, start, end)[0]


def load_funding(cache_dir="cache", start="2021-01", end="2025-12"):
    """Exact archive event times, rates, intervals and an explicit mark-price proxy."""
    return build_cache(cache_dir, start, end)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-01")
    parser.add_argument("--end", default="2025-12")
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--refresh", action="store_true", help="Check current source checksums")
    parser.add_argument("--report", default="data-check.json")
    parser.add_argument("--manifest", default="data-manifest.json")
    args = parser.parse_args()
    data, funding, checks, manifest = build_cache(args.cache, args.start, args.end, args.refresh)
    Path(args.report).write_text(json.dumps(checks, indent=2) + "\n")
    Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Ready: {len(data):,} hourly rows and {len(funding):,} funding events.")


if __name__ == "__main__":
    main()
