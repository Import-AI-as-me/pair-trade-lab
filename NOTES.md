# Method notes

## Question and dates

Does a moving hedge help a pair trade after costs and funding?

The study uses Binance BTCUSDT and ETHUSDT USD-M perpetual contracts. It compares fixed OLS, rolling OLS, and Kalman hedges. All use one shared price signal. Two checks add a Kalman entry gate and a smaller Kalman position. This is a test of known methods on one pair, not a claim of a new method or a stable arbitrage.

| Use | Dates | Choices allowed |
| --- | --- | --- |
| Training | 2021–2022 | Fixed hedge, scales, initial Kalman state |
| Validation | 2023 | Shared entry level and smaller-position size |
| Test | 2024–2025 | Results only |

Choose the entry level from `1, 1.5, 2` by the fixed hedge's validation net Sharpe at 5 bp cost. A tie picks the higher level. All methods use that choice. Other settings are fixed before the test.

Both validation and test start with no holdings. The models retain earlier price history and can learn from new data as it arrives. They cannot use future data. These are known past years, so the date split is not a live test.

## Shared signal

Let `y = log(ETH close)` and `x = log(BTC close)`. Fit an intercept and slope on training data. Keep the slope `beta_fixed` for the signal:

```text
s[k] = y[k] - beta_fixed * x[k]
z[k] = (s[k] - mean(s[k-720:k])) / std(s[k-720:k])
```

The mean and sample standard deviation use the prior 720 hours, excluding `s[k]`. A fixed intercept cancels when taking the difference from the mean.

When flat, a high positive `z` means short ETH and long BTC. A low negative `z` means the reverse. Close when `z` crosses zero or the trade reaches 72 hours. Check these rules only at the trading times below.

The rolling mean can move without a price reversal. A zero crossing is a close rule, not proof of mean reversion. Changes in the hedge or its fitted intercept cannot change this shared signal.

## Hedge models

Fixed OLS keeps the training slope. Rolling OLS fits a new intercept and slope to the latest 720 known hourly closes. An invalid hedge closes any position.

The Kalman model is:

```text
y[k] = alpha[k] + beta[k] * x[k] + observation_noise[k]
[alpha[k], beta[k]] = [alpha[k-1], beta[k-1]] + state_noise[k]
```

Center log prices on their first training values. Start with the training OLS state. Let `X` be the training matrix with an intercept column, and `R` the training residual variance:

```text
P_initial = R * inverse(X.T @ X)
Q = R * 0.0001 * diag(1, 1 / variance(training_x))
```

These covariance matrices need a full-rank `X` and positive training variance. Their scale is a model choice, not proof that errors follow the model.

The filter starts updating in 2023. Each update uses one known close. It uses the Joseph covariance update and never a backward smoother. Slopes used for trading are limited to `[0.1, 3]`; the output counts values outside this range. The Kalman intercept affects the hedge only.

## Entry gate and smaller size

The gate allows a new trade only when `beta_sd / abs(beta) <= 0.25`, using the raw Kalman slope and its known state covariance. The gate does not force an open trade to close.

This is uncertainty within the model, not a chance of making money. A gate can reduce losses simply by trading less. The smaller-size check scales the full Kalman position by the ratio of gated to full Kalman average gross exposure in validation, capped at one. It aims to match validation exposure. Costs and changes in equity can leave a small gap. The scale is frozen for test, where exposure and risk may differ sharply.

## Timing and holdings

Each row starts at a UTC hour. Its close is near that hour's end. An order at open `t` uses closes only through row `t-2`, leaving a full hour after the observed close. Signal, hedge, and gate all use this cutoff. Rebalance only at UTC hours `01, 05, 09, 13, 17, 21`. Funding history never sets orders.

A log-price slope is a dollar hedge ratio. For direction `d`, with `+1` meaning long ETH:

```text
ETH_weight = d / (1 + abs(beta))
BTC_weight = -d * beta / (1 + abs(beta))
units[i] = weight[i] * equity_before_cost / open[i]
```

A full trade targets total absolute notional equal to equity before cost. It need not be dollar neutral. Coin units stay fixed between rebalances.

```text
price_pnl = sum(old_units[i] * (open[i] - previous_open[i]))
turnover = sum(abs(new_units[i] - old_units[i]) * open[i])
cost = turnover * cost_bps / 10000
```

Equity changes only through price PnL, funding, and costs. A new hedge is a trade, not profit. Never use changes in a fitted spread as cash returns. Costs cover entries, exits, and changes in holdings. Cases use 0, 5, and 10 bp per leg per one-way traded notional; 5 bp is the main case. These are assumed total costs, not measured fills.

The test ends at the 2025-12-31 23:00 UTC open. Close all remaining units there and charge one close cost. Use no later price.

## Funding and data

At each event hour:

```text
funding_cash = -sum(old_units[i] * mark_open[i] * funding_rate[i])
```

A positive rate costs longs and pays shorts. Charge the units held before any same-hour rebalance. Floor the archive event time to its UTC hour. Use that hour's mark-price open as a proxy for the exact funding mark.

The loader checks that this sample has a complete eight-hour funding schedule and rejects missing or changed intervals. Actual recorded rates set the payments. They are not past forecasts available before settlement. Binance's API can provide exact payment marks; this study uses hourly mark files instead. [Binance API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

The price grid contains all 43,824 hours. Ten official daily mark files supply gaps in monthly files. Downloads have source links and checked SHA-256 hashes. These checks verify files, not every market record. Binance may later revise its archive. [Public data](https://github.com/binance/binance-public-data)

## Metrics and limits

Sharpe uses daily net returns, sample standard deviation, and a 365-day year, with zero cash yield. Maximum drawdown uses hourly equity. The main comparison is Kalman minus fixed daily return. Paired circular block resampling gives 95% bounds using 7, 14, and 28-day blocks. These bounds keep fitted choices fixed and do not cover all model-choice error.

Candles do not show bids, asks, depth, or real fills. This test cannot establish trade size limits or exact costs. It does not model margin calls, liquidation, or venue failure. A positive result on one pair would need more evidence; a loss remains a valid result.

## Prior work

Kalman filtering is a standard method: [Kalman (1960), original paper](https://www.cs.unc.edu/~welch/kalman/media/pdf/Kalman1960.pdf).

Close prior work studies Kalman pair rules and a neural extension on daily ETF and currency data: [Milstein et al. (2023 version)](https://arxiv.org/html/2210.15448v2). This project uses neither its neural model nor its reward formula. The focus here is a shared signal, separate leg holdings, delayed orders, funding, and stated costs.
