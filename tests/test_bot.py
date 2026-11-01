from datetime import date

import numpy as np
import pandas as pd
import pytest

import bot
import common as C

TODAY = date(2026, 10, 26)
TS = pd.Timestamp(TODAY)


def series(values, end=TS):
    idx = pd.date_range(end=end, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


# -------------------------------------------------------------------- analyse
def test_analyse_needs_history():
    assert bot.analyse(series([10] * 5), TS) is None             # <7 giorni precedenti
    assert bot.analyse(series([10] * 8), TS) is not None


def test_analyse_flat_series():
    st = bot.analyse(series([10.0] * 60), TS)
    assert st["price"] == 10 and st["med30"] == 10 and st["chg7"] == 0
    assert st["max90"] == 10
    assert st["z"] == 0                                           # std minima 2%: niente divisioni per 0


def test_analyse_spike():
    st = bot.analyse(series([10.0] * 59 + [15.0]), TS)
    assert st["price"] == 15
    assert st["chg7"] == pytest.approx(50)
    assert st["z"] > 2
    assert st["med30"] == 10 and st["max90"] == 15


def test_analyse_chg7_requires_recent_enough_reference():
    # ultimo dato di 7+ giorni fa piu' vecchio di 14 giorni -> nessun confronto a 7 giorni
    idx = list(pd.date_range(end=TS - pd.Timedelta(days=20), periods=10)) + [TS]
    s = pd.Series([10.0] * 10 + [12.0], index=idx)
    assert bot.analyse(s, TS)["chg7"] is None


def test_analyse_unsorted_input():
    s = series([10.0] * 59 + [15.0]).sample(frac=1, random_state=1)
    assert bot.analyse(s, TS)["price"] == 15


# ---------------------------------------------------------------- sell_signal
def test_sell_signal_all_conditions():
    st = {"price": 15, "med30": 10, "z": 3.0, "chg7": 50, "max90": 15}
    score, reasons = bot.sell_signal(st)
    assert score == 4 and len(reasons) == 4


def test_sell_signal_none():
    st = {"price": 10, "med30": 10, "z": 0.1, "chg7": 1, "max90": 20}
    assert bot.sell_signal(st) == (0, [])


def test_sell_signal_handles_missing_metrics():
    st = {"price": 10, "med30": None, "z": None, "chg7": None, "max90": 10}
    score, _ = bot.sell_signal(st)
    assert score == 1                                             # solo "vicino al massimo"


def test_sell_signal_thresholds_are_inclusive():
    st = {"price": 12.5, "med30": 10, "z": None, "chg7": 20, "max90": 99}
    assert bot.sell_signal(st)[0] == 2


# ---------------------------------------------------------------- build_series
def test_build_series_without_cm():
    g = pd.DataFrame({"date": ["2026-10-20", "2026-10-21"], "market": [5.0, 6.0]})
    s = bot.build_series(g, None)
    assert s.tolist() == [5.0, 6.0]


def test_build_series_rescales_cm_history():
    ct = pd.DataFrame({"date": ["2026-10-20", "2026-10-21"], "market": [10.0, 11.0]})
    cm = pd.DataFrame({"date": ["2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"],
                       "cm_price": [4.0, 4.0, 5.0, 5.0, 5.5]})
    s = bot.build_series(ct, cm)
    # rapporto = 10 / 5 (cm al 20/10) = 2 -> storico precedente riscalato
    assert s.loc["2026-10-17"] == 8.0 and s.loc["2026-10-19"] == 10.0
    assert s.loc["2026-10-20"] == 10.0 and s.loc["2026-10-21"] == 11.0   # i dati CT restano intatti
    assert s.index.is_monotonic_increasing and not s.index.has_duplicates


def test_build_series_cm_starts_after_ct():
    ct = pd.DataFrame({"date": ["2026-10-01"], "market": [10.0]})
    cm = pd.DataFrame({"date": ["2026-10-05"], "cm_price": [4.0]})
    assert bot.build_series(ct, cm).tolist() == [10.0]


def test_build_series_cm_zero_price_ignored():
    ct = pd.DataFrame({"date": ["2026-10-20"], "market": [10.0]})
    cm = pd.DataFrame({"date": ["2026-10-19"], "cm_price": [0.0]})
    assert bot.build_series(ct, cm).tolist() == [10.0]


# --------------------------------------------------------------- load_inventory
def test_load_inventory_filters(monkeypatch):
    products = [
        {"id": 1, "game_id": 1, "user_id": 7, "blueprint_id": 100, "name_en": "Bolt", "quantity": 3,
         "price_cents": 250, "properties_hash": {"mtg_foil": True, "mtg_language": "it", "condition": "Mint"}},
        {"id": 2, "game_id": 2, "blueprint_id": 5},                                   # altro gioco
        {"id": 3, "game_id": 1, "blueprint_id": 6, "graded": True},                   # gradata
        {"id": 4, "game_id": 1, "blueprint_id": 7, "graded": "false", "bundle_size": 1, "user_id": 7},
        {"id": 5, "game_id": 1, "blueprint_id": 8, "bundle_size": 4},                 # bundle
    ]
    monkeypatch.setattr(C, "ct_get", lambda *a, **k: products)
    inv = bot.load_inventory()
    assert [i["id"] for i in inv] == [1, 4]
    a, b = inv
    assert a["foil"] == 1 and a["language"] == "it" and a["price"] == 2.5 and a["quantity"] == 3
    assert b["foil"] == 0 and b["language"] == "en" and b["condition"] == "Near Mint"
    assert b["name"] == "Blueprint 7" and b["quantity"] == 1


def test_market_snapshot_uses_reference_price(monkeypatch):
    from conftest import listing
    monkeypatch.setattr(C, "ct_get", lambda *a, **k: {"100": [listing(1), listing(2), listing(3, user=7)]})
    snap = bot.market_snapshot(100, 0, "en", my_user_id=7)
    assert snap["n"] == 2 and snap["min"] == 1
    monkeypatch.setattr(C, "ct_get", lambda *a, **k: [])
    assert bot.market_snapshot(100, 0, "en", 7) is None


# --------------------------------------------------------------------- messaggi
def test_label_and_section():
    assert bot.label("Bolt", 1, "it") == "Bolt (foil, IT)"
    assert bot.label("Bolt", 0, "en") == "Bolt (EN)"
    assert bot.section("T", []) == ""
    assert bot.section("T", ["a", "b"]) == "T\na\nb\n"


def test_ban_lines_marks_owned_cards():
    changes = [{"format": "modern", "banned": ["Bolt", "Ponder"], "unbanned": ["Mox"]},
               {"format": "pauper", "banned": [], "unbanned": []}]
    lines = bot.ban_lines(changes, {"bolt"})
    assert lines == ["- Modern - BANDITE: Bolt (la possiedi!), Ponder", "- Modern - LIBERATE: Mox"]
