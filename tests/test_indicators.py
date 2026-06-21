"""Test della logica EMA e del conteggio chiusure consecutive."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.indicators import consecutive_closes_beyond_ema, ema


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_ema_seed_is_sma():
    values = [1, 2, 3, 4, 5]
    result = ema(values, 5)
    assert result[:4] == [None, None, None, None]
    assert approx(result[4], 3.0)  # SMA dei primi 5


def test_ema_known_values():
    values = [1, 2, 3, 4, 5, 6]
    result = ema(values, 5)
    # seed SMA = 3.0 ; mult = 2/6 ; next = (6-3)*(1/3)+3 = 4.0
    assert approx(result[5], 4.0)


def test_ema_too_short():
    assert ema([1, 2], 5) == [None, None]


def test_consecutive_above():
    closes = [10, 9, 11, 12, 13]
    ema_series = [None, 9.5, 10.0, 10.5, 11.0]
    # ultime tre: 11>10, 12>10.5, 13>11 -> ma serve continuita' dall'ultima
    assert consecutive_closes_beyond_ema(closes, ema_series, "above") == 3


def test_consecutive_breaks():
    closes = [12, 8, 11, 12]
    ema_series = [9.0, 9.0, 10.0, 11.0]
    # ultima 12>11 (1), 11>10 (2), 8>9? no -> stop
    assert consecutive_closes_beyond_ema(closes, ema_series, "above") == 2


def test_consecutive_below():
    closes = [5, 4, 3]
    ema_series = [6.0, 6.0, 6.0]
    assert consecutive_closes_beyond_ema(closes, ema_series, "below") == 3


def test_consecutive_none_ema_stops():
    closes = [11, 12]
    ema_series = [None, 10.0]
    # ultima 12>10 (1), poi ema None -> stop
    assert consecutive_closes_beyond_ema(closes, ema_series, "above") == 1


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} test passati")
    sys.exit(1 if failed else 0)
