# Data notes

This project uses public BTCUSDT and ETHUSDT perpetual futures data from
[Binance Vision](https://data.binance.vision/). These are USD-M contracts quoted
in USDT. The default sample is January 2021 through December 2025, inclusive.
Prices are hourly. All times are UTC.

Run `python get_data.py` after installing the project requirements. The script
downloads monthly `klines`, `markPriceKlines`, and `fundingRate` archives. It uses
six download workers and checks every ZIP against its official SHA256
`.CHECKSUM` file. The source format and checksum process are described in the
[official archive guide](https://github.com/binance/binance-public-data).

Some monthly mark-price files omit days that are present in the daily archive.
The loader gets the missing hours from the official daily ZIPs, with the same
checksum check. The report records those dates and the manifest includes each
daily source. This adds observed prices; it does not estimate missing bars.

The first run needs internet access. Later runs use the saved ZIPs and checksum
files and still check their hashes. Use `python get_data.py --refresh` to check
the latest source checksums. Binance can revise old archives. The published
`data-manifest.json` records the URLs and hashes used for this study.

For a smaller download, run:

```sh
python get_data.py --start 2024-01 --end 2024-12
```

This command replaces the local CSVs and reports for the chosen range. It does
not remove other months from the archive cache. The cache is ignored by Git.
No account, key, private data, or trading endpoint is used.

## Output

`load_data(cache_dir="cache", start="2021-01", end="2025-12")` returns a pandas
DataFrame with a UTC timestamp index. Each row starts one hourly price bar.
For each of `BTC` and `ETH`, the columns are:

| Column suffix | Meaning |
| --- | --- |
| `_open` | First trade price in the hour |
| `_close` | Last trade price in the hour |
| `_mark_open` | First mark price in the hour |
| `_funding` | Sum of funding rates with calculation times in `[t, t + 1 hour)` |

`load_funding(...)` returns the separate funding event table. Its UTC index
keeps the archive's `calc_time`, including millisecond offsets. Columns are
`symbol`, `funding_rate`, `funding_interval_hours`, and `mark_open_proxy`.
Events for both symbols can have the same timestamp. A positive funding rate
means a long position pays a short position.

Both loaders check the saved source files and write `cache/joined.csv` and
`cache/funding.csv`. The command also writes `data-check.json` and
`data-manifest.json` at the project root. These small reports can be shared;
the raw data stays in `cache/`.

## Funding and timing

The funding archives contain rates and calculation times but no settlement
mark price. `mark_open_proxy` is the mark-price open of the event's hour.
It is a price approximation, not an exact funding-fee record. The public
[funding-history API documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
describes a separate settlement mark field; this project does not use that API.

The study treats the funding calculation as the scheduled hour boundary. It
charges the position held just before that boundary, then changes the position
at the hourly trade open. Small calculation-time offsets do not allow a new
position to avoid or collect the previous position's payment. A fee in USDT is
approximated by `units held * mark_open_proxy * funding_rate`. The recorded
rate is not a signal known before the event.

Returns between consecutive opens end at the next row's open. The final price
row has no next open inside this sample, so it cannot supply another full
hourly return. A bar's close is only available when that bar ends.

## Checks

The loader accepts price archives with or without a header. It detects
millisecond or microsecond timestamps and checks candle close times, positive
OHLC values, ordering, duplicates, and all expected hours. It does not apply
the 2025 spot timestamp change to futures by date alone.

The funding check requires one event in each expected eight-hour window and
an eight-hour interval in the source. Missing events, a changed schedule,
price hours still missing after daily supplements, or a failed checksum stop
the run. Prices and rates are never forward-filled. Zero hourly funding means
no event in that hour after
the full funding schedule has passed the check.

The checked full sample has 43,824 hourly rows and 5,478 funding events per
symbol. It uses 360 monthly ZIPs and 10 daily mark-price ZIPs. All timestamps
in these files are milliseconds. Funding calculation times fall 0 to 47
milliseconds after the scheduled hour. There are no remaining gaps or duplicate
timestamps within either symbol.

## Source terms and attribution

Source: Binance Vision, BTCUSDT and ETHUSDT USD-M monthly price, mark-price,
and funding-rate archives. Changes made here: timestamp parsing, hourly joins,
funding sums, checks, and research calculations and charts.

The [Binance Vision Dataset Terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md),
dated August 26, 2026, apply to the data and derived research outputs. Those
outputs are shared under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
Keep this attribution and the same license when sharing them. This project is
for non-commercial research and education. Binance does not sponsor or endorse
it. The dataset terms do not by themselves choose a license for this project's
original source code.
