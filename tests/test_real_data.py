"""Controlli di coerenza sui file veri in data/ (sola lettura)."""
import json
import pathlib
from datetime import date

import numpy as np
import pandas as pd
import pytest

import bot
import box_ev as B
import calendar_events as E
import mtgjson_data as MJ

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "data"


@pytest.fixture(scope="module")
def prices():
    return pd.read_csv(D / "prices.csv")


@pytest.fixture(scope="module")
def hist():
    return pd.read_csv(D / "history_mtgjson.csv")


@pytest.fixture(scope="module")
def boxes():
    return pd.read_csv(D / "box_ev.csv")


@pytest.mark.realdata
def test_schemas(prices, hist, boxes):
    assert list(prices.columns) == bot.PRICE_COLS
    assert list(hist.columns) == ["date", "blueprint_id", "foil", "cm_price"]
    assert set(B.RESULT_COLS) <= set(boxes.columns)
    assert list(pd.read_csv(D / "card_info.csv").columns) == MJ.INFO_COLS
    assert list(pd.read_csv(D / "events.csv").columns) == E.EVENT_COLS
    assert list(pd.read_csv(D / "alerts_log.csv").columns) == ["date", "type", "key"]
    assert set(json.load(open(D / "bans_snapshot.json"))) <= set(E.BAN_FORMATS)


@pytest.mark.realdata
def test_prices_integrity(prices):
    assert not prices.duplicated(["date", "blueprint_id", "foil", "language"]).any()
    assert prices[["market", "min", "n_listings"]].notna().all().all()
    assert (prices["market"] > 0).all() and (prices["min"] > 0).all()
    assert (prices["min"] <= prices["market"] + 1e-9).all()           # il minimo non supera la mediana dei 3 piu' bassi
    assert set(prices["foil"]) <= {0, 1}
    pd.to_datetime(prices["date"])                                    # date valide


@pytest.mark.realdata
def test_prices_continuity(prices):
    days = pd.to_datetime(sorted(prices["date"].unique()))
    gaps = pd.Series(days).diff().dt.days.dropna()
    assert (gaps <= 1).all(), f"giorni mancanti nello storico: {gaps[gaps > 1].tolist()}"


@pytest.mark.realdata
def test_history_integrity(hist):
    assert not hist.duplicated(["blueprint_id", "foil", "date"]).any()
    assert (hist["cm_price"] > 0).all() and set(hist["foil"]) <= {0, 1}


@pytest.mark.realdata
def test_box_results_integrity(boxes):
    b = boxes.dropna(subset=["ratio"])
    assert not b.duplicated(["date", "set_code", "product"]).any()
    calc = (b["ev_net"] / b["box_price"]).round(3)
    assert ((calc - b["ratio"]).abs() < 0.01).all()                   # ratio coerente con EV e prezzo
    assert b["prob_profit"].between(0, 1).all() and b["coverage"].between(0, 1).all()
    assert ((b["p10"] <= b["median"] + 1e-9) & (b["median"] <= b["p90"] + 1e-9)).all()
    assert (b["box_price"] > 0).all() and (b["ev_net"] >= 0).all()


@pytest.mark.realdata
def test_inventory_cards_have_info_and_history(prices, hist):
    bps = set(prices["blueprint_id"])
    info = set(pd.read_csv(D / "card_info.csv")["blueprint_id"])
    cm = set(hist["blueprint_id"])
    print(f"carte monitorate {len(bps)}: con info {len(bps & info)}, con storico CM {len(bps & cm)}")
    assert len(bps & cm) / len(bps) > 0.9
    assert len(bps & info) / len(bps) > 0.9


@pytest.mark.realdata
def test_replay_analysis_on_real_history(prices, hist):
    """Rigioca build_series + analyse + sell_signal su ogni carta reale: nessun crash, valori sensati."""
    cm_by_key = {k: g for k, g in hist.groupby(["blueprint_id", "foil"])}
    today = pd.Timestamp(prices["date"].max())
    n = analysed = signals = 0
    for key, g in prices.groupby(["blueprint_id", "foil", "language"]):
        n += 1
        s = bot.build_series(g, cm_by_key.get((key[0], key[1])))
        assert s.index.is_monotonic_increasing and not s.index.has_duplicates
        assert np.isfinite(s.values).all() and (s > 0).all()
        st = bot.analyse(s, today)
        if st is None:
            continue
        analysed += 1
        assert st["max90"] >= st["price"] - 1e-9
        score, reasons = bot.sell_signal(st)
        assert 0 <= score <= 4 and len(reasons) == score
        signals += score >= bot.SELL_MIN_SCORE
    print(f"serie {n}, analizzabili {analysed}, con segnale vendi {signals}")
    assert analysed / n > 0.9


@pytest.mark.realdata
def test_risk_labels_on_real_card_info():
    info = MJ.load_card_info.__wrapped__() if hasattr(MJ.load_card_info, "__wrapped__") else None
    df = pd.read_csv(D / "card_info.csv", dtype=str).fillna("")
    labelled = sum(bool(MJ.risk_labels(r, date(2026, 10, 26))) for r in df.to_dict("records"))
    assert labelled > 0


@pytest.mark.realdata
def test_events_real_file_dates_valid():
    ev = pd.read_csv(D / "events.csv")
    s, e = pd.to_datetime(ev["start"]), pd.to_datetime(ev["end"])
    assert (e >= s).all()
    assert not ev.duplicated(["type", "name", "start"]).any()
