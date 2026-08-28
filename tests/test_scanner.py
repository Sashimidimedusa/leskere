"""Test dello scanner. Nessuna rete: tutte le candele sono sintetiche.

    python3 -m unittest tests.test_scanner -v
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.bybit import Candle, drop_unclosed
from scanner.engine import prescore_ticker
from scanner.features import Features, compute_features
from scanner.indicators import atr, ema, pct_change, percentile_rank, rvol, scale, vwap
from scanner.score import build_plan, infer_direction, score_symbol
from scanner.store import Store
from scanner.tracker import evaluate_signal

MS_15M = 15 * 60_000


def flat_features(**overrides):
    """Features neutre: nessun trend, nessuna rottura, nessuna spinta."""
    base = dict(
        symbol="FLATUSDT", price=100.0, turnover_24h=5e7, spread_bp=2.0,
        atr_pct_15m=0.002, atr_pct_1h=0.006, atr_abs_1h=0.6, squeeze_pct=0.5,
        rvol_1h=1.0, ret_1h=0.0, ret_4h=0.0, ret_24h=0.0, rs_btc_4h=0.0,
        range_pos_24h=0.5, breakout_atr=0.0, ema_align_15m=0, ema_align_1h=0,
        vwap_dist_atr=0.0, oi_change_1h=0.0, funding_rate=0.0001,
    )
    base.update(overrides)
    return Features(**base)


def make_candles(n, start_price=100.0, drift=0.0, noise=0.0, volume=1000.0,
                 interval_ms=MS_15M, t0=1_700_000_000_000):
    """Serie deterministica: drift per barra + oscillazione sinusoidale."""
    candles = []
    price = start_price
    for i in range(n):
        wobble = noise * math.sin(i / 3.0)
        open_p = price
        price = price * (1 + drift) + wobble
        high = max(open_p, price) + abs(noise) * 0.5 + 0.01
        low = min(open_p, price) - abs(noise) * 0.5 - 0.01
        candles.append(Candle(
            start=t0 + i * interval_ms,
            open=open_p, high=high, low=low, close=price,
            volume=volume, turnover=volume * price,
        ))
    return candles


class TestIndicatori(unittest.TestCase):
    def test_ema_costante(self):
        # Su una serie costante la EMA vale esattamente quel valore.
        self.assertAlmostEqual(ema([5.0] * 30, 10), 5.0, places=9)

    def test_ema_formula(self):
        values = [1, 2, 3, 4, 5]
        # seed = SMA(1,2,3) = 2 ; k = 2/4 = 0.5
        # -> (4-2)*0.5+2 = 3 ; (5-3)*0.5+3 = 4
        self.assertAlmostEqual(ema(values, 3), 4.0, places=9)

    def test_ema_dati_insufficienti(self):
        self.assertIsNone(ema([1.0, 2.0], 10))

    def test_atr_range_costante(self):
        # Barre con escursione costante di 2: l'ATR deve valere 2.
        highs = [102.0] * 30
        lows = [100.0] * 30
        closes = [101.0] * 30
        self.assertAlmostEqual(atr(highs, lows, closes, 14), 2.0, places=9)

    def test_atr_none_se_corto(self):
        self.assertIsNone(atr([1.0] * 5, [0.0] * 5, [0.5] * 5, 14))

    def test_rvol_raddoppio(self):
        # 100 barre a volume 10, poi 4 barre a volume 20 -> RVOL = 2
        volumes = [10.0] * 100 + [20.0] * 4
        self.assertAlmostEqual(rvol(volumes, 4, 24), 2.0, places=9)

    def test_rvol_usa_mediana_non_media(self):
        # Una singola finestra storica enorme non deve schiacciare l'RVOL:
        # con la media il risultato sarebbe molto sotto 1.
        volumes = [10.0] * 40 + [10_000.0] * 4 + [10.0] * 56 + [20.0] * 4
        self.assertAlmostEqual(rvol(volumes, 4, 24), 2.0, places=6)

    def test_rvol_dati_insufficienti(self):
        self.assertIsNone(rvol([1.0] * 10, 4, 24))

    def test_percentile_rank(self):
        self.assertAlmostEqual(percentile_rank([1, 2, 3, 4], 3), 0.75)

    def test_vwap_pesa_i_volumi(self):
        # Barra a prezzo 10 con volume 1, barra a prezzo 20 con volume 3 -> 17.5
        v = vwap([10, 20], [10, 20], [10, 20], [1, 3])
        self.assertAlmostEqual(v, 17.5, places=9)

    def test_pct_change(self):
        self.assertAlmostEqual(pct_change([100, 110], 1), 0.1, places=9)

    def test_scale_satura_e_gestisce_none(self):
        self.assertEqual(scale(None, 0, 1), 0.0)
        self.assertEqual(scale(5, 0, 1), 1.0)
        self.assertEqual(scale(-5, 0, 1), 0.0)
        self.assertAlmostEqual(scale(0.5, 0, 1), 0.5)


class TestCandeleChiuse(unittest.TestCase):
    def test_scarta_la_candela_in_corso(self):
        candles = make_candles(5, t0=0)
        # "adesso" cade dentro l'ultima barra: va scartata.
        now = candles[-1].start + MS_15M // 2
        self.assertEqual(len(drop_unclosed(candles, 15, now_ms=now)), 4)

    def test_tiene_tutte_le_barre_chiuse(self):
        candles = make_candles(5, t0=0)
        now = candles[-1].start + MS_15M + 1
        self.assertEqual(len(drop_unclosed(candles, 15, now_ms=now)), 5)


class TestFeature(unittest.TestCase):
    def setUp(self):
        self.c15 = make_candles(300, drift=0.002, noise=0.05)
        self.c1h = make_candles(150, drift=0.008, noise=0.2, interval_ms=60 * 60_000)
        self.ticker = {"turnover24h": "5e7", "bid1Price": "100", "ask1Price": "100.02",
                       "fundingRate": "0.0001"}

    def test_feature_trend_rialzista(self):
        f = compute_features("TESTUSDT", self.c15, self.c1h, self.ticker, btc_ret_4h=0.0)
        self.assertIsNotNone(f)
        self.assertEqual(f.ema_align_15m, 1)
        self.assertGreater(f.ret_4h, 0)
        self.assertGreater(f.rs_btc_4h, 0)          # sale mentre BTC e' fermo
        self.assertAlmostEqual(f.spread_bp, 2.0, places=1)

    def test_breakout_positivo_su_nuovi_massimi(self):
        f = compute_features("TESTUSDT", self.c15, self.c1h, self.ticker)
        # Serie monotona crescente: il prezzo e' sempre oltre il massimo passato.
        self.assertGreater(f.breakout_atr, 0)
        self.assertGreater(f.range_pos_24h, 0.9)

    def test_none_se_storia_troppo_corta(self):
        self.assertIsNone(compute_features("X", make_candles(10), make_candles(5), {}))

    def test_oi_change(self):
        oi = [{"timestamp": i, "openInterest": 100.0 + i} for i in range(20)]
        f = compute_features("TESTUSDT", self.c15, self.c1h, self.ticker, oi_series=oi)
        # da 107 a 119 su 12 campioni
        self.assertAlmostEqual(f.oi_change_1h, (119 - 107) / 107, places=6)


class TestDirezioneEScore(unittest.TestCase):
    def _features(self, drift, noise=0.05, volume=1000.0, btc=0.0, ticker=None):
        c15 = make_candles(300, drift=drift, noise=noise, volume=volume)
        c1h = make_candles(150, drift=drift * 4, noise=noise * 4, interval_ms=60 * 60_000)
        return compute_features("TESTUSDT", c15, c1h,
                                ticker or {"turnover24h": "5e7"}, btc_ret_4h=btc)

    def test_direzione_long_su_trend_rialzista(self):
        self.assertEqual(infer_direction(self._features(0.002)), "long")

    def test_direzione_short_su_trend_ribassista(self):
        self.assertEqual(infer_direction(self._features(-0.002)), "short")

    def test_nessuna_direzione_se_i_voti_si_annullano(self):
        # Contesto e spinta si contraddicono: meglio nessuna chiamata che una a caso.
        f = flat_features()
        self.assertIsNone(infer_direction(f))

    def test_score_basso_su_mercato_piatto(self):
        # Un mercato che oscilla senza andare da nessuna parte non deve mai
        # arrivare in cima alla classifica, qualunque direzione venga dedotta.
        opp = score_symbol(self._features(0.0, noise=0.02))
        if opp is not None:
            self.assertLess(opp.score, 25)

    def test_score_piu_alto_con_trend_forte(self):
        debole = score_symbol(self._features(0.0004, noise=0.02))
        forte = score_symbol(self._features(0.003, noise=0.05))
        self.assertIsNotNone(forte)
        if debole is not None:
            self.assertGreater(forte.score, debole.score)

    def test_spread_largo_penalizza(self):
        stretto = score_symbol(self._features(0.002, ticker={"turnover24h": "5e7", "bid1Price": "100", "ask1Price": "100.02"}))
        largo = score_symbol(self._features(0.002, ticker={"turnover24h": "5e7", "bid1Price": "100", "ask1Price": "100.5"}))
        self.assertLess(largo.score, stretto.score)
        self.assertIn("spread", largo.penalties)

    def test_score_entro_0_100(self):
        opp = score_symbol(self._features(0.003))
        self.assertGreaterEqual(opp.score, 0)
        self.assertLessEqual(opp.score, 100)

    def test_reasons_non_vuote_su_setup_forte(self):
        opp = score_symbol(self._features(0.003))
        self.assertTrue(opp.reasons)


class TestPianoOperativo(unittest.TestCase):
    def _f(self, direction="long"):
        drift = 0.002 if direction == "long" else -0.002
        c15 = make_candles(300, drift=drift, noise=0.05)
        c1h = make_candles(150, drift=drift * 4, noise=0.2, interval_ms=60 * 60_000)
        return compute_features("TESTUSDT", c15, c1h, {"turnover24h": "5e7"})

    def test_rapporto_rischio_rendimento(self):
        plan = build_plan(self._f(), "long", stop_atr_mult=1.5, target_atr_mult=3.0)
        self.assertAlmostEqual(plan.risk_reward, 2.0, places=6)

    def test_lati_corretti_long(self):
        plan = build_plan(self._f(), "long")
        self.assertLess(plan.stop, plan.entry)
        self.assertGreater(plan.target, plan.entry)

    def test_lati_corretti_short(self):
        plan = build_plan(self._f("short"), "short")
        self.assertGreater(plan.stop, plan.entry)
        self.assertLess(plan.target, plan.entry)


class TestVerificaEsiti(unittest.TestCase):
    def _c(self, highs, lows, closes):
        return [Candle(start=i * MS_15M, open=c, high=h, low=l, close=c, volume=1, turnover=1)
                for i, (h, l, c) in enumerate(zip(highs, lows, closes))]

    def test_target_colpito(self):
        out = evaluate_signal(self._c([101, 104], [99, 100], [100, 103]),
                              "long", entry=100, stop=98, target=103)
        self.assertEqual(out["hit"], "target")
        self.assertAlmostEqual(out["ret"], 0.03, places=6)
        self.assertAlmostEqual(out["mfe"], 2.0, places=6)     # (104-100)/2 R

    def test_stop_colpito(self):
        out = evaluate_signal(self._c([101, 100], [99, 97], [100, 97.5]),
                              "long", entry=100, stop=98, target=103)
        self.assertEqual(out["hit"], "stop")
        self.assertLess(out["ret"], 0)

    def test_barra_ambigua_conta_come_stop(self):
        # Una sola barra che tocca sia stop sia target: regola conservativa.
        out = evaluate_signal(self._c([104], [97], [100]), "long", entry=100, stop=98, target=103)
        self.assertEqual(out["hit"], "stop")

    def test_short_guadagna_quando_scende(self):
        out = evaluate_signal(self._c([100, 99], [98, 95], [99, 96]),
                              "short", entry=100, stop=102, target=94)
        self.assertGreater(out["ret"], 0)

    def test_nessuna_candela(self):
        out = evaluate_signal([], "long", 100, 98, 103)
        self.assertEqual(out["hit"], "none")


class TestPrescore(unittest.TestCase):
    def test_premia_chi_si_muove(self):
        fermo = prescore_ticker({"lastPrice": "100", "prevPrice1h": "100",
                                 "price24hPcnt": "0.001", "turnover24h": "5e7",
                                 "highPrice24h": "101", "lowPrice24h": "99"})
        mosso = prescore_ticker({"lastPrice": "103", "prevPrice1h": "100",
                                 "price24hPcnt": "0.08", "turnover24h": "5e7",
                                 "highPrice24h": "103", "lowPrice24h": "95"})
        self.assertGreater(mosso[0], fermo[0])

    def test_prezzo_non_valido(self):
        self.assertIsNone(prescore_ticker({"lastPrice": "0"}))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.store.close()

    def _opp(self, score=70.0):
        c15 = make_candles(300, drift=0.002, noise=0.05)
        c1h = make_candles(150, drift=0.008, noise=0.2, interval_ms=60 * 60_000)
        opp = score_symbol(compute_features("TESTUSDT", c15, c1h, {"turnover24h": "5e7"}))
        opp.score = score
        return opp

    def test_inserisce_e_rilegge(self):
        sid = self.store.insert_signal(self._opp())
        self.assertGreater(sid, 0)
        rows = self.store.recent_signals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "TESTUSDT")

    def test_cooldown(self):
        self.store.insert_signal(self._opp())
        self.assertTrue(self.store.recent_signal_exists("TESTUSDT", "long", 120))
        self.assertFalse(self.store.recent_signal_exists("TESTUSDT", "short", 120))

    def test_performance_per_fascia(self):
        for score, ret in ((75.0, 0.02), (35.0, -0.01)):
            sid = self.store.insert_signal(self._opp(score))
            self.store.record_outcome(sid, 240, price_at=1.0, ret=ret, mfe=1.0, mae=-0.2, hit="none")
        buckets = {b["bucket"]: b for b in self.store.performance(240)}
        self.assertAlmostEqual(buckets[70]["avg_ret"], 0.02, places=6)
        self.assertAlmostEqual(buckets[30]["avg_ret"], -0.01, places=6)
        self.assertEqual(self.store.summary()["evaluated_4h"], 2)

    def test_pending_evaluations_solo_se_maturi(self):
        vecchio = int(time.time() * 1000) - 5 * 3600 * 1000
        self.store.insert_signal(self._opp(), ts_ms=vecchio)
        self.store.insert_signal(self._opp())     # adesso: non ancora maturo
        pending = self.store.pending_evaluations((60,))
        self.assertEqual(len(pending), 1)


class TestTrackerFinestra(unittest.TestCase):
    """Regressione: un segnale vecchio va valutato sulle candele DEL SUO periodo.

    Chiedendo semplicemente "le ultime N barre" si otterrebbe una finestra che
    non contiene affatto il segnale (scanner riavviato, arretrati da smaltire,
    orizzonte a 8h), e l'esito non verrebbe mai registrato.
    """

    class _Client:
        def __init__(self):
            self.last_end_ms = None

        def get_klines(self, symbol, interval, limit=200, use_cache=True, end_ms=None):
            self.last_end_ms = end_ms
            step = interval * 60_000
            end = end_ms if end_ms is not None else int(time.time() * 1000)
            return make_candles(400, drift=0.001, noise=0.02,
                                interval_ms=step, t0=end - 400 * step)[-limit:]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.store.close()

    def test_valuta_un_segnale_di_ore_prima(self):
        from scanner.tracker import OutcomeTracker

        c15 = make_candles(300, drift=0.002, noise=0.05)
        c1h = make_candles(150, drift=0.008, noise=0.2, interval_ms=60 * 60_000)
        opp = score_symbol(compute_features("TESTUSDT", c15, c1h, {"turnover24h": "5e7"}))

        vecchio = int(time.time() * 1000) - 9 * 3600 * 1000     # 9 ore fa
        self.store.insert_signal(opp, ts_ms=vecchio)

        client = self._Client()
        done = OutcomeTracker(client, self.store).run_once((60,))
        self.assertEqual(done, 1)
        # la richiesta deve essere ancorata alla fine dell'orizzonte, non ad adesso
        self.assertLess(client.last_end_ms, int(time.time() * 1000) - 7 * 3600 * 1000)


class TestBacktest(unittest.TestCase):
    def test_replay_produce_osservazioni(self):
        from scanner.backtest import aggregate, replay_symbol

        c15 = make_candles(600, drift=0.0015, noise=0.05)
        c1h = make_candles(200, drift=0.006, noise=0.2, interval_ms=60 * 60_000)
        rows = replay_symbol("TESTUSDT", c15, c1h, c15, step_bars=8)
        self.assertTrue(rows)
        self.assertTrue(all("score" in r for r in rows))
        table = aggregate(rows, "4h")
        self.assertTrue(table)
        self.assertTrue(all(0 <= r["win_rate"] <= 1 for r in table))

    def test_replay_non_guarda_nel_futuro(self):
        # Storia crescente e poi crollo: le valutazioni fatte PRIMA del crollo
        # non devono conoscerlo (lo score resta alto pur essendo poi sbagliato).
        salita = make_candles(500, drift=0.002, noise=0.05)
        rows_solo_salita = None
        from scanner.backtest import replay_symbol

        c1h = make_candles(200, drift=0.008, noise=0.2, interval_ms=60 * 60_000)
        rows_a = replay_symbol("T", salita, c1h, salita, step_bars=8)

        crollo = list(salita)
        last = crollo[-1]
        for i in range(50):
            p = last.close * (0.97 ** (i + 1))
            crollo.append(Candle(start=last.start + (i + 1) * MS_15M, open=p * 1.03,
                                 high=p * 1.03, low=p * 0.99, close=p, volume=1000, turnover=1000))
        rows_b = replay_symbol("T", crollo, c1h, crollo, step_bars=8)

        comuni = {r["ts"]: r["score"] for r in rows_a}
        for r in rows_b:
            if r["ts"] in comuni:
                self.assertAlmostEqual(r["score"], comuni[r["ts"]], places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
