"""Small checks with known cash flows and strict information cutoffs.

Run with: python -m unittest -v test_study
No downloads or real market data are used.
"""
import unittest

import numpy as np
import pandas as pd

import study


LEGS = ['BTC', 'ETH']


def cash_fixture(n=4):
    index = pd.date_range('2024-01-01', periods=n, freq='h', tz='UTC')
    prices = pd.DataFrame({'BTC': 100., 'ETH': 50.}, index=index)
    funding = pd.DataFrame(0., index=index, columns=LEGS)
    marks = prices.copy()
    weights = pd.DataFrame(np.nan, index=index, columns=LEGS)
    return prices, funding, marks, weights


def feature_fixture(n=12):
    index = pd.date_range('2024-01-01', periods=n, freq='h', tz='UTC')
    return pd.DataFrame({'z': 0., 'fixed': 1., 'roll': 1.,
                         'kalman': 1., 'beta_sd': .05}, index=index)


def price_fixture(n=120):
    index = pd.date_range('2022-01-01', periods=n, freq='h', tz='UTC')
    k = np.arange(n, dtype=float)
    log_btc = 4.6 + .003*k + .025*np.sin(k/5.)
    log_eth = 1. + .85*log_btc + .04*np.sin(k/2.7) + .008*np.cos(k/8.)
    btc, eth = np.exp(log_btc), np.exp(log_eth)
    return pd.DataFrame({
        'BTC_close': btc, 'ETH_close': eth,
        'BTC_open': btc, 'ETH_open': eth,
        'BTC_mark_open': btc*1.001, 'ETH_mark_open': eth*.999,
        'BTC_funding': 0., 'ETH_funding': 0.,
    }, index=index)


class CashAccountingTests(unittest.TestCase):
    def test_changed_hedge_at_flat_prices_has_no_gross_profit(self):
        prices, funding, marks, weights = cash_fixture()
        weights.iloc[0] = [-.5, .5]             # Dollar hedge ratio 1.
        weights.iloc[1] = [-2/3, 1/3]           # Dollar hedge ratio 2.
        weights.iloc[2] = [-1/3, 2/3]           # Dollar hedge ratio 0.5.
        log = study.book(prices, funding, marks, weights, cost_bps=10.)
        np.testing.assert_array_equal(log.price_pnl, 0.)
        self.assertGreater(log.cost.iloc[1], 0.)
        self.assertGreater(log.cost.iloc[2], 0.)
        self.assertLess(log.equity.iloc[-1], 1.)
        self.assertAlmostEqual(log.equity.iloc[-1], 1.-log.cost.sum(), places=13)

    def test_positive_funding_debits_long_and_credits_short_at_mark_price(self):
        for sign in (1., -1.):
            with self.subTest(sign=sign):
                prices, funding, marks, weights = cash_fixture(3)
                weights.iloc[0] = [0., sign*.5]  # Signed 0.01 ETH.
                funding.iloc[1, 1] = .02
                marks.iloc[1, 1] = 70.         # Deliberately not trade price 50.
                log = study.book(prices, funding, marks, weights,
                                 cost_bps=0., close_last=False)
                self.assertAlmostEqual(log.funding_pnl.iloc[1], -sign*.014, places=14)
                self.assertAlmostEqual(log.equity.iloc[-1], 1.-sign*.014, places=14)
                self.assertAlmostEqual(log.price_pnl.sum(), 0., places=14)

    def test_funding_uses_old_units_before_a_flip(self):
        prices, funding, marks, weights = cash_fixture(3)
        weights.iloc[0] = [0., .5]
        weights.iloc[1] = [0., -.5]
        funding.iloc[1:, 1] = .02
        marks.iloc[1:, 1] = 70.
        log = study.book(prices, funding, marks, weights,
                         cost_bps=0., close_last=False)
        # First debit uses the old +0.01 ETH, not the newly opened short.
        self.assertAlmostEqual(log.funding_pnl.iloc[1], -.014, places=14)
        self.assertAlmostEqual(log.ETH_units.iloc[1], -.00986, places=14)
        self.assertAlmostEqual(log.funding_pnl.iloc[2], .013804, places=14)

    def test_entry_does_not_receive_or_pay_old_position_funding(self):
        prices, funding, marks, weights = cash_fixture(3)
        weights.iloc[0] = [0., .5]
        funding.iloc[:2, 1] = .02
        log = study.book(prices, funding, marks, weights,
                         cost_bps=0., close_last=False)
        self.assertEqual(log.funding_pnl.iloc[0], 0.)
        self.assertAlmostEqual(log.funding_pnl.iloc[1], -.01, places=14)

    def test_flip_pays_for_both_legs_and_both_directions(self):
        prices, funding, marks, weights = cash_fixture(2)
        weights.iloc[0] = [-.5, .5]
        weights.iloc[1] = [.5, -.5]
        log = study.book(prices, funding, marks, weights,
                         cost_bps=10., close_last=False)
        # Entry: 1 USDT turnover and 0.001 fee. Equity before the flip is .999.
        # Closing 1 USDT and opening .999 USDT gives 1.999 turnover.
        self.assertAlmostEqual(log.turnover.iloc[0], 1., places=14)
        self.assertAlmostEqual(log.turnover.iloc[1], 1.999, places=14)
        self.assertAlmostEqual(log.cost.iloc[1], .001999, places=14)
        self.assertAlmostEqual(log.equity.iloc[-1], .997001, places=14)

    def test_final_close_cost_uses_current_notional_once(self):
        prices, funding, marks, weights = cash_fixture(3)
        prices['BTC'] = [100., 110., 120.]
        marks['BTC'] = prices.BTC
        weights.iloc[0] = [.5, 0.]
        weights.iloc[-1] = [1., 0.]             # Final close overrides this order.
        log = study.book(prices, funding, marks, weights, cost_bps=10.)
        np.testing.assert_allclose(log.price_pnl, [0., .05, .05], atol=1e-14)
        np.testing.assert_allclose(log.cost, [.0005, 0., .0006], atol=1e-14)
        self.assertAlmostEqual(log.equity.iloc[-1], 1.0989, places=14)
        self.assertEqual(log.BTC_units.iloc[-1], 0.)
        self.assertEqual(log.ETH_units.iloc[-1], 0.)

    def test_final_funding_is_paid_before_final_close(self):
        prices, funding, marks, weights = cash_fixture(3)
        weights.iloc[0] = [0., .5]
        funding.iloc[-1, 1] = .02
        log = study.book(prices, funding, marks, weights, cost_bps=0.)
        self.assertAlmostEqual(log.funding_pnl.iloc[-1], -.01, places=14)
        self.assertAlmostEqual(log.equity.iloc[-1], .99, places=14)
        self.assertEqual(log.ETH_units.iloc[-1], 0.)

    def test_no_order_keeps_coin_units_while_weights_drift(self):
        prices, funding, marks, weights = cash_fixture(3)
        prices['BTC'] = [100., 110., 120.]
        marks['BTC'] = prices.BTC
        weights.iloc[0] = [.5, 0.]
        log = study.book(prices, funding, marks, weights,
                         cost_bps=0., close_last=False)
        np.testing.assert_allclose(log.BTC_units, [.005, .005, .005], atol=1e-14)
        np.testing.assert_allclose(log.turnover, [.5, 0., 0.], atol=1e-14)
        self.assertAlmostEqual(log.equity.iloc[-1], 1.1, places=14)
        self.assertAlmostEqual(log.gross.iloc[-1], .6/1.1, places=14)
        self.assertNotAlmostEqual(log.gross.iloc[-1], .5, places=8)

    def test_open_to_open_move_belongs_to_the_carried_position(self):
        prices, funding, marks, weights = cash_fixture(3)
        prices['BTC'] = [100., 120., 120.]
        marks['BTC'] = prices.BTC
        weights.iloc[0] = [.5, 0.]
        weights.iloc[1] = [0., 0.]
        log = study.book(prices, funding, marks, weights, cost_bps=0.)
        np.testing.assert_allclose(log.price_pnl, [0., .1, 0.], atol=1e-14)
        self.assertAlmostEqual(log.equity.iloc[-1], 1.1, places=14)

    def test_cash_identity_and_reported_returns_agree(self):
        prices, funding, marks, weights = cash_fixture(4)
        prices['BTC'] = [100., 104., 98., 103.]
        prices['ETH'] = [50., 49., 53., 52.]
        marks = prices*1.01
        funding.iloc[2] = [.001, -.002]
        weights.iloc[0] = [-.5, .5]
        weights.iloc[2] = [.25, -.75]
        log = study.book(prices, funding, marks, weights, cost_bps=7.)
        expected = 1.+(log.price_pnl+log.funding_pnl-log.cost).cumsum()
        np.testing.assert_allclose(log.equity, expected, atol=1e-13)
        self.assertAlmostEqual(float((1.+log['return']).prod()), log.equity.iloc[-1], places=13)

    def test_invalid_values_and_partial_orders_are_rejected(self):
        for bad in ('missing_price', 'negative_mark', 'one_missing_weight'):
            with self.subTest(bad=bad):
                prices, funding, marks, weights = cash_fixture(3)
                weights.iloc[0] = [-.5, .5]
                if bad == 'missing_price':
                    prices.iloc[1, 0] = np.nan
                elif bad == 'negative_mark':
                    marks.iloc[1, 0] = -1.
                else:
                    weights.iloc[1] = [0., np.nan]
                with self.assertRaises(ValueError):
                    study.book(prices, funding, marks, weights)

    def test_weight_columns_cannot_silently_swap_the_legs(self):
        prices, funding, marks, weights = cash_fixture(3)
        weights.iloc[0] = [-.25, .75]
        with self.assertRaises(ValueError):
            study.book(prices, funding, marks, weights[['ETH', 'BTC']])


class InformationTimingTests(unittest.TestCase):
    def test_close_shock_first_trades_two_rows_later(self):
        features = feature_fixture(8)
        features.loc[features.index[2], 'z'] = 3.
        orders = study.targets(features, 'fixed', 1., '2024-01-01', '2024-01-02',
                               trade_hours=tuple(range(24)))
        np.testing.assert_array_equal(orders.iloc[:4].to_numpy(), 0.)
        np.testing.assert_allclose(orders.iloc[4], [.5, -.5], atol=1e-14)
        # The close in row 2 arrives near 03:00; execution is at 04:00.
        self.assertEqual((orders.index[4]-features.index[2]).total_seconds(), 7200.)

    def test_orders_use_only_the_fixed_rebalance_hours(self):
        features = feature_fixture(24)
        features['z'] = 3.
        orders = study.targets(features, 'fixed', 1., '2024-01-01', '2024-01-02')
        actual = set(orders.index[orders.notna().all(axis=1)].hour)
        self.assertEqual(actual, {1, 5, 9, 13, 17, 21})
        self.assertTrue(orders.loc[orders.index.hour == 8].isna().all().all())

    def test_gate_blocks_an_entry_but_does_not_force_an_exit(self):
        features = feature_fixture(8)
        features['z'] = 3.
        features['beta_sd'] = [.5, .1, 2., 2., 2., 2., 2., 2.]
        orders = study.targets(features, 'gate', 1., '2024-01-01', '2024-01-02',
                               trade_hours=tuple(range(24)))
        np.testing.assert_allclose(orders.iloc[2], [0., 0.], atol=1e-14)
        np.testing.assert_allclose(orders.iloc[3], [.5, -.5], atol=1e-14)
        np.testing.assert_allclose(orders.iloc[4], [.5, -.5], atol=1e-14)

    def test_signal_window_excludes_its_current_close(self):
        data = price_fixture()
        features, info = study.fit(data, lookback=12, train_end='2022-01-02 16:00:00')
        spread = np.log(data.ETH_close)-info['fixed_beta']*np.log(data.BTC_close)
        past = spread.iloc[43:55].to_numpy()
        expected = (spread.iloc[55]-past.mean())/past.std(ddof=1)
        self.assertAlmostEqual(features.z.iloc[55], expected, places=10)

    def test_future_closes_cannot_change_past_signals_or_orders(self):
        data = price_fixture()
        train_end = '2022-01-02 16:00:00'
        features, info = study.fit(data, lookback=12, train_end=train_end)
        changed = data.copy()
        # At row 61's open, row 60's close is still outside the allowed cutoff.
        k = np.arange(len(data)-60, dtype=float)
        changed.loc[changed.index[60]:, 'BTC_close'] *= np.exp(.15+.002*k)
        changed.loc[changed.index[60]:, 'ETH_close'] *= np.exp(-.25-.003*k)
        later, changed_info = study.fit(changed, lookback=12, train_end=train_end)
        self.assertEqual(info, changed_info)
        pd.testing.assert_frame_equal(features.iloc[:60], later.iloc[:60])
        self.assertGreater(abs(features.kalman.iloc[60]-later.kalman.iloc[60]), 1e-5)
        for method in ('fixed', 'roll', 'kalman', 'gate', 'small'):
            with self.subTest(method=method):
                orders = study.targets(features, method, .8, train_end, '2022-01-06')
                new_orders = study.targets(later, method, .8, train_end, '2022-01-06')
                cutoff = data.index[61]
                pd.testing.assert_frame_equal(orders.loc[:cutoff], new_orders.loc[:cutoff])
                if method != 'gate':
                    self.assertTrue((orders.loc[:cutoff].abs().sum(axis=1) > 0.).any())
                ledger, _ = study.run_one(data, features, method, .8, train_end, '2022-01-06')
                new_ledger, _ = study.run_one(changed, later, method, .8, train_end, '2022-01-06')
                pd.testing.assert_frame_equal(ledger.loc[:cutoff], new_ledger.loc[:cutoff])


if __name__ == '__main__':
    unittest.main()
