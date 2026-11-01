from datetime import date

import numpy as np
import pandas as pd
import pytest

import box_ev as B
import common as C
from conftest import listing

TODAY = date(2026, 10, 26)
KEEP = 1 - B.SELL_FEE_PCT / 100


def ref(low, n=5, market=None):
    return {"low": low, "market": market or low, "min": low, "n": n}


def cfg(sheets, boosters):
    return {"sheets": sheets, "boosters": boosters}


def sheet(cards, foil=False, fixed=False):
    return {"cards": cards, "foil": foil, "fixed": fixed}


# -------------------------------------------------------------------- make_ref
def test_make_ref_outlier_low_replaced_by_second():
    ls = [listing(p) for p in (3, 10, 10.5, 11)]
    r = B.make_ref(ls)
    assert r["low"] == 10 and r["min"] == 3 and r["second"] == 10


def test_make_ref_normal():
    r = B.make_ref([listing(p) for p in (10, 10.5, 11)])
    assert r["low"] == 10 and r["second"] == 10.5 and r["n"] == 3


def test_make_ref_single_listing_and_none():
    r = B.make_ref([listing(5)])
    assert r["low"] == 5 and r["second"] is None
    assert B.make_ref([]) is None


# ---------------------------------------------------------- find_box_blueprint
BPS = [
    {"id": 1, "name": "Test Set Play Booster Box"},
    {"id": 2, "name": "Test Set Play Booster Box Case"},
    {"id": 3, "name": "Test Set Collector Booster Box"},
    {"id": 4, "name": "Test Set Prerelease Pack"},
    {"id": 5, "name": "Test Set Bundle"},
]


def test_find_box_blueprint_play_and_collector():
    assert B.find_box_blueprint(BPS, ["play", "booster"])["id"] == 1        # niente case
    assert B.find_box_blueprint(BPS, ["collector", "booster"])["id"] == 3


def test_find_box_blueprint_plain_fallback_only_if_unique():
    plain = [{"id": 9, "name": "Ancient Realm Booster Box"}]
    assert B.find_box_blueprint(plain, ["play", "booster"]) is None
    assert B.find_box_blueprint(plain, ["play", "booster"], plain_fallback=True)["id"] == 9
    two = plain + [{"id": 10, "name": "Ancient Realm Booster Box Sealed"}]
    assert B.find_box_blueprint(two, ["play", "booster"], plain_fallback=True) is None


def test_find_box_blueprint_fallback_excludes_collector_and_draft():
    bps = [{"id": 1, "name": "X Collector Booster Box"}, {"id": 2, "name": "X Draft Booster Box"}]
    assert B.find_box_blueprint(bps, ["play", "booster"], plain_fallback=True) is None


# ------------------------------------------------------------- packs_per_box
def test_packs_per_box():
    sd = {"sealedProduct": [
        {"name": "Test Play Booster Box Case", "contents": {"sealed": [{"name": "Test Play Booster Box", "count": 6}]}},
        {"name": "Test Play Booster Box", "contents": {"sealed": [{"name": "Test Play Booster Pack", "count": 36}]}},
    ]}
    assert B.packs_per_box(sd, ["play", "booster"], 30) == (36, "mtgjson")
    assert B.packs_per_box(sd, ["collector", "booster"], 12) == (12, "predefinito")
    assert B.packs_per_box({}, ["play", "booster"], 30) == (30, "predefinito")


def test_load_mtgjson_set_reserved_windows_names(monkeypatch):
    urls = []
    monkeypatch.setattr(C, "web_get_json", lambda url, **k: urls.append(url) or {"data": {"ok": 1}})
    assert B.load_mtgjson_set("con") == {"ok": 1}
    assert urls[0].endswith("/CON_.json")
    monkeypatch.setattr(C, "web_get_json", lambda url, **k: None)
    assert B.load_mtgjson_set("zzz") is None


# ----------------------------------------------------------------- recent_sets
SETS = [
    {"code": "new", "name": "New", "released_at": "2026-09-01", "set_type": "expansion"},
    {"code": "old", "name": "Old", "released_at": "2025-01-01", "set_type": "expansion"},
    {"code": "fut", "name": "Future", "released_at": "2027-01-01", "set_type": "expansion"},
    {"code": "dig", "name": "Digital", "released_at": "2026-09-01", "set_type": "expansion", "digital": True},
    {"code": "tok", "name": "Token", "released_at": "2026-09-01", "set_type": "token"},
    {"code": "sub", "name": "Sub", "released_at": "2026-09-01", "set_type": "expansion", "parent_set_code": "new"},
    {"code": "mas", "name": "Masters", "released_at": "2026-03-01", "set_type": "masters"},
    {"code": "nod", "name": "NoDate", "set_type": "expansion"},
]


def test_recent_sets(monkeypatch):
    monkeypatch.setattr(C, "scryfall_sets", lambda: SETS)
    out = B.recent_sets(TODAY)
    assert [s["code"] for s in out] == ["new", "mas"]                    # ordinati dal piu' recente


def test_companion_sets(monkeypatch):
    extra = SETS + [{"code": "cmd", "name": "Commander", "released_at": "2026-09-02", "set_type": "commander"}]
    monkeypatch.setattr(C, "scryfall_sets", lambda: extra)
    main = {"code": "new", "name": "New", "released": date(2026, 9, 1)}
    codes = [s["code"] for s in B.companion_sets(main)]
    assert "cmd" in codes and "new" not in codes and "tok" not in codes and "dig" not in codes


def test_related_expansions_and_expansion_index(monkeypatch):
    monkeypatch.setattr(C, "ct_get", lambda *a, **k: [
        {"id": 1, "game_id": 1, "code": "NEW", "name": "New"},
        {"id": 2, "game_id": 1, "code": "newsld", "name": "New: Sealed"},
        {"id": 3, "game_id": 2, "code": "x", "name": "New Yugioh"}])
    by_code, by_name, exps = B.ct_expansion_index()
    assert by_code == {"new": 1, "newsld": 2} and by_name["new"] == 1 and len(exps) == 2
    assert B.related_expansions("New", 1, exps) == [{"id": 2, "name": "New: Sealed"}]


# ------------------------------------------------------------------ build_model
def make_model(prices_nf, cm=None, cm_all=False, robust=True, foil_sheet=False, names=None):
    booster = cfg(
        {"common": sheet({"c1": 1, "c2": 1}), "rare": sheet({"r1": 1})},
        [{"contents": {"common": 2, "rare": 1}, "weight": 1}])
    uuid2sf = {"c1": "sc1", "c2": "sc2", "r1": "sr1"}
    sf2bp = {"sc1": 11, "sc2": 12, "sr1": 13}
    return B.build_model(booster, uuid2sf, sf2bp, prices_nf, {}, names or {}, robust=robust,
                         cm_prices=cm, cm_all=cm_all)


def test_model_exact_pack_ev():
    nf = {11: ref(1.0), 12: ref(1.0), 13: ref(10.0)}
    m = make_model(nf)
    ev, cov = B.pack_ev_and_coverage(m)
    assert ev == pytest.approx(KEEP * (2 * 1.0 + 10.0))
    assert cov == 1.0


def test_model_cards_below_min_value_are_worthless():
    nf = {11: ref(0.10), 12: ref(0.29), 13: ref(10.0)}
    ev, _ = B.pack_ev_and_coverage(make_model(nf))
    assert ev == pytest.approx(KEEP * 10.0)


def test_model_thin_market_discounts():
    nf = {11: ref(1.0, n=1), 12: ref(1.0, n=2), 13: ref(10.0, n=5)}
    ev, _ = B.pack_ev_and_coverage(make_model(nf))
    assert ev == pytest.approx(KEEP * (0.5 * 1.0 + 0.75 * 1.0 + 10.0))


def test_model_cardmarket_fills_missing_and_caps_thin():
    nf = {11: ref(5.0, n=1), 13: ref(10.0, n=5)}               # c2 senza offerte su CT
    cm = {"c1": {"normal": 2.0}, "c2": {"normal": 3.0}}
    m = make_model(nf, cm=cm)
    ev, cov = B.pack_ev_and_coverage(m)
    # c1: poche offerte e CM (2.0) < 70% di 5.0 -> uso CM, senza sconto; c2: riempita con CM
    assert ev == pytest.approx(KEEP * (2.0 + 3.0 + 10.0))
    assert cov == 1.0
    assert m["cm_stats"] == {"cm_fill": 1, "cm_cap": 1, "cm_ok": 0}


def test_model_cardmarket_ok_when_close():
    nf = {11: ref(5.0, n=1), 12: ref(5.0, n=5), 13: ref(10.0, n=5)}
    m = make_model(nf, cm={"c1": {"normal": 4.9}})
    assert m["cm_stats"]["cm_ok"] == 1 and m["cm_stats"]["cm_cap"] == 0


def test_model_cm_all_takes_min_everywhere():
    nf = {11: ref(5.0, n=5), 12: ref(5.0, n=5), 13: ref(10.0, n=5)}
    ev, _ = B.pack_ev_and_coverage(make_model(nf, cm={"r1": {"normal": 4.0}}, cm_all=True))
    assert ev == pytest.approx(KEEP * (5 + 5 + 4.0))


def test_model_not_robust_uses_market_and_ignores_cm():
    nf = {11: {"market": 2.0, "low": 1.0, "min": 1.0, "n": 5}, 12: ref(1.0), 13: ref(10.0)}
    ev, _ = B.pack_ev_and_coverage(make_model(nf, robust=False, cm={"c1": {"normal": 0.5}}))
    assert ev == pytest.approx(KEEP * (2.0 + 1.0 + 10.0))


def test_model_coverage_and_basic_land():
    nf = {13: ref(10.0)}
    m = make_model(nf, names={"c1": "Forest", "c2": "Mystery Card"})
    ev, cov = B.pack_ev_and_coverage(m)
    assert cov == pytest.approx((1 * 1.0 + 1 * 0.5 + 1 * 1.0) / 3 * 0 + (2 * 0.5 + 1) / 3, abs=1e-9)
    summary, details = B.coverage_report(m)
    assert "senza blueprint" in summary or "senza offerte" in summary or summary == ""


def test_model_foil_sheet_falls_back_to_nonfoil():
    booster = cfg({"f": sheet({"c1": 1}, foil=True)}, [{"contents": {"f": 1}, "weight": 1}])
    m = B.build_model(booster, {"c1": "s"}, {"s": 1}, {1: ref(4.0)}, {}, {})
    assert B.pack_ev_and_coverage(m)[0] == pytest.approx(KEEP * 4.0)
    m = B.build_model(booster, {"c1": "s"}, {"s": 1}, {1: ref(4.0)}, {1: ref(9.0)}, {})
    assert B.pack_ev_and_coverage(m)[0] == pytest.approx(KEEP * 9.0)


def test_model_fixed_sheet():
    booster = cfg({"land": sheet({"a": 1, "b": 1}, fixed=True), "c": sheet({"c1": 1})},
                  [{"contents": {"land": 1, "c": 1}, "weight": 1}])
    m = B.build_model(booster, {"a": "sa", "b": "sb", "c1": "sc"}, {"sa": 1, "sb": 2, "sc": 3},
                      {1: ref(1.0), 2: ref(2.0), 3: ref(4.0)}, {}, {})
    assert B.pack_ev_and_coverage(m)[0] == pytest.approx(KEEP * (1 + 2 + 4))


def test_model_boosters_as_dict_and_zero_weights():
    booster = {"sheets": {"c": sheet({"c1": 1})},
               "boosters": {"a": [{"contents": {"c": 1}, "weight": 3}, {"contents": {"c": 1}, "weight": 0}]}}
    m = B.build_model(booster, {"c1": "s"}, {"s": 1}, {1: ref(2.0)}, {}, {})
    assert len(m["configs"]) == 1 and m["configs"][0]["weight"] == 3


def test_model_empty_inputs():
    m = B.build_model({"sheets": {}, "boosters": []}, {}, {}, {}, {}, {})
    assert m["sheets"] == {} and m["configs"] == []
    assert B.pack_ev_and_coverage(m) == (0.0, 0.0)


# ---------------------------------------------------------- simulazione / EV
def realistic_model():
    cards = {f"c{i}": 1 for i in range(80)}
    rares = {f"r{i}": (1 if i < 12 else 3) for i in range(15)}
    uuid2sf = {u: "s" + u for u in list(cards) + list(rares)}
    sf2bp, prices = {}, {}
    for i, u in enumerate(list(cards) + list(rares)):
        sf2bp["s" + u] = i
        prices[i] = ref(0.2 + (i % 7) * 0.3 if u.startswith("c") else 2 + i * 1.5)
    booster = cfg({"common": sheet(cards), "rare": sheet(rares)},
                  [{"contents": {"common": 9, "rare": 1}, "weight": 9},
                   {"contents": {"common": 8, "rare": 2}, "weight": 1}])
    return B.build_model(booster, uuid2sf, sf2bp, prices, {}, {})


def test_simulation_mean_matches_exact_ev():
    m = realistic_model()
    packs = 36
    exact = B.pack_ev_and_coverage(m)[0] * packs
    totals = B.simulate_boxes(m, packs, 40000, np.random.default_rng(1))
    assert len(totals) == 40000 and (totals >= 0).all()
    assert totals.mean() == pytest.approx(exact, rel=0.02)


def test_simulation_fixed_sheet_adds_constant():
    booster = cfg({"land": sheet({"a": 1}, fixed=True)}, [{"contents": {"land": 1}, "weight": 1}])
    m = B.build_model(booster, {"a": "s"}, {"s": 1}, {1: ref(2.0)}, {}, {})
    totals = B.simulate_boxes(m, 10, 100, np.random.default_rng(0))
    assert np.allclose(totals, 10 * KEEP * 2.0)


def test_evaluate_product_metrics():
    m = realistic_model()
    r = B.evaluate_product(m, 36, 100.0, np.random.default_rng(0))
    assert r["ev_net"] > 0 and r["ratio"] == pytest.approx(r["ev_net"] / 100)
    assert 0 <= r["prob_profit"] <= 1 and r["p10"] <= r["median"] <= r["p90"]
    assert 0 <= r["coverage"] <= 1
    assert B.evaluate_product(m, 36, 0.0, np.random.default_rng(0))["ratio"] == 0.0


def test_evaluate_product_deterministic_with_seed():
    m = realistic_model()
    a = B.evaluate_product(m, 36, 100.0, np.random.default_rng(5))
    b = B.evaluate_product(m, 36, 100.0, np.random.default_rng(5))
    assert a == b


def test_top_contributors_shares():
    m = realistic_model()
    names = {f"r{i}": f"Rare {i}" for i in range(15)}
    top, share, thin = B.top_contributors(m, 36, names, n=5)
    assert len(top) == 5 and 0 < share <= 1 and thin == 0
    assert [t[3] for t in top] == sorted((t[3] for t in top), reverse=True)
    assert share == pytest.approx(sum(t[3] for t in top))
    assert top[0][0].startswith("Rare")                         # le rare pesano piu' delle comuni


def test_top_contributors_empty_when_all_worthless():
    nf = {11: ref(0.1), 12: ref(0.1), 13: ref(0.1)}
    assert B.top_contributors(make_model(nf), 36, {}) == ([], 0.0, 0.0)


def test_coverage_report_details():
    nf = {13: ref(10.0)}
    m = make_model(nf, names={"c1": "Card Uno", "c2": "Card Due"})
    summary, details = B.coverage_report(m)
    assert "senza blueprint" not in summary                     # c1/c2 hanno blueprint ma non offerte
    assert "senza offerte" in summary
    assert details and "Card" in details[0]


# ------------------------------------------------------------- is_alert / format
def result(**kw):
    r = {"code": "tst", "set": "Test", "product": "Play Booster Box", "box_price": 100.0, "box_second": 105.0,
         "packs": 36, "packs_source": "mtgjson", "ev_net": 120.0, "ratio": 1.2, "prob_profit": 0.6,
         "median": 110.0, "p10": 70.0, "p90": 180.0, "coverage": 0.97, "ratio_median": 1.1, "ratio_cm_min": 0.9,
         "top": [("Rare A", 20.0, 4, 0.3, 18.0, 20.0)], "top_share": 0.3, "thin_share": 0.0,
         "cm_stats": {"cm_fill": 1, "cm_cap": 2, "cm_ok": 3}}
    r.update(kw)
    return r


def test_is_alert_rules():
    assert B.is_alert(result())
    assert not B.is_alert(result(ratio=1.09))
    assert B.is_alert(result(ratio=B.EV_ALERT_RATIO))
    assert not B.is_alert(result(coverage=0.8))
    assert not B.is_alert(result(packs_source="predefinito"))


def test_format_line_contains_key_info():
    t = B.format_line(result())
    for s in ("Test - Play Booster Box", "100.00 EUR", "x1.20", "36 bustine", "Rare A", "Cardmarket"):
        assert s in t


def test_format_line_warns_on_suspicious_cheap_box():
    t = B.format_line(result(box_price=50.0, box_second=100.0))
    assert "ATTENZIONE" in t and "50%" in t
    assert "ATTENZIONE" not in B.format_line(result())


def test_format_line_handles_minimal_result():
    r = result()
    for k in ("top", "cm_stats", "box_second", "ratio_median", "ratio_cm_min", "thin_share"):
        r.pop(k)
    assert "Test - Play Booster Box" in B.format_line(r)


# ----------------------------------------------------------------- save_results
def test_save_results_replaces_same_day(workdir):
    B.save_results([result()], TODAY)
    B.save_results([result(box_price=90.0)], TODAY)                   # rilancio nello stesso giorno
    B.save_results([result()], date(2026, 10, 27))
    df = pd.read_csv("data/box_ev.csv")
    assert len(df) == 2 and list(df.columns) == B.RESULT_COLS
    assert df[df["date"] == "2026-10-26"]["box_price"].iloc[0] == 90.0


def test_save_results_empty(workdir):
    B.save_results([], TODAY)
    assert pd.read_csv("data/box_ev.csv").empty
