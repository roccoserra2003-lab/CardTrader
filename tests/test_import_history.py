import gzip
import io
import json

import pandas as pd
import pytest

import common as C
import import_history as IH
import mtgjson_data as MJ


def stream_of(obj):
    return io.BytesIO(json.dumps(obj).encode())


PRICES = {"meta": {}, "data": {
    "u1": {"paper": {"cardmarket": {"retail": {"normal": {"2026-10-01": 1.0, "2026-10-02": 1.5}, "foil": {"2026-10-01": 4.0}}},
                     "tcgplayer": {"retail": {"normal": {"2026-10-01": 9.0}}}}},
    "u2": {"paper": {"tcgplayer": {"retail": {"normal": {"2026-10-01": 2.0}}}}},
    "u3": {"paper": {}},
    "u4": {"paper": {"cardmarket": {"retail": {"normal": {"2026-10-01": 3.0}}}}},
}}


def test_read_prices_filters_and_stats():
    stats = {}
    found = IH.read_prices(stream_of(PRICES), {"u1", "u2", "u3"}, stats)
    assert set(found) == {"u1", "u2", "u3"}
    assert found["u1"]["cardmarket"]["normal"] == {"2026-10-01": 1.0, "2026-10-02": 1.5}
    assert stats["uuid_trovati"] == 3


def test_read_prices_stops_early_when_all_found():
    stats = {}
    IH.read_prices(stream_of(PRICES), {"u1"}, stats)
    assert stats["uuid_letti"] == 1                      # non legge il resto del file enorme


def test_pick_prices_provider_preference_and_finish():
    prov = {}
    s = IH.read_prices(stream_of(PRICES), {"u1", "u2"}, {})
    assert IH.pick_prices(s["u1"], 0, prov) == {"2026-10-01": 1.0, "2026-10-02": 1.5}   # cardmarket batte tcgplayer
    assert IH.pick_prices(s["u1"], 1, prov) == {"2026-10-01": 4.0}                      # foil
    assert IH.pick_prices(s["u2"], 0, prov) == {"2026-10-01": 2.0}                      # fallback tcgplayer
    assert IH.pick_prices(s["u2"], 1, prov) == {}                                       # nessun dato foil
    assert prov == {"cardmarket": 2, "tcgplayer": 1}


def test_my_card_keys(monkeypatch):
    monkeypatch.setattr(C, "ct_get", lambda *a, **k: [
        {"game_id": 1, "blueprint_id": 1, "properties_hash": {"mtg_foil": "true"}},
        {"game_id": 1, "blueprint_id": 1, "properties_hash": {}},
        {"game_id": 1, "blueprint_id": 2, "graded": True},
        {"game_id": 2, "blueprint_id": 3},
        {"game_id": 1, "blueprint_id": 4, "bundle_size": 3},
    ])
    assert IH.my_card_keys() == {(1, 1), (1, 0)}


def test_blueprint_info(monkeypatch):
    def ct(path, params=None, **k):
        if path == "/expansions/export":
            return [{"id": 10, "game_id": 1, "code": "AAA", "name": "A"}, {"id": 11, "game_id": 2, "code": "Y"}]
        return [{"id": 1, "scryfall_id": "sf1"}, {"id": 2, "scryfall_id": None}, {"id": 3, "scryfall_id": "sf3"}]
    monkeypatch.setattr(C, "ct_get", ct)
    assert IH.blueprint_info({1, 2}, []) == {1: ("sf1", "AAA")}


def test_blueprint_info_survives_expansion_errors(monkeypatch):
    def ct(path, params=None, **k):
        if path == "/expansions/export":
            return [{"id": 10, "game_id": 1, "code": "AAA", "name": "A"}]
        raise RuntimeError("500")
    monkeypatch.setattr(C, "ct_get", ct)
    notes = []
    assert IH.blueprint_info({1}, notes) == {} and "blueprint non letti" in notes[0]


MJ_SET = {"cards": [{"uuid": "u1", "name": "Bolt", "identifiers": {"scryfallId": "sf1"}, "printings": ["LEA", "M10"],
                     "isReserved": False, "edhrecRank": 5, "legalities": {"standard": "Legal"}},
                    {"uuid": "u2", "name": "NoSf", "identifiers": {}}]}


def test_scryfall_to_uuid_and_fallback(monkeypatch):
    calls = []

    def loader(code):
        calls.append(code)
        return MJ_SET if code == "real" else None
    monkeypatch.setattr(IH, "load_mtgjson_set", loader)
    monkeypatch.setattr(C, "web_get_json", lambda url, **k: {"set": "real"})
    mapping, cards = IH.scryfall_to_uuid({1: ("sf1", "weird")}, [])
    assert mapping == {"sf1": "u1"} and cards["u1"]["standard"] and cards["u1"]["edhrec"] == 5
    assert calls == ["weird", "real"]


def test_scryfall_to_uuid_missing_set(monkeypatch):
    monkeypatch.setattr(IH, "load_mtgjson_set", lambda code: None)
    monkeypatch.setattr(C, "web_get_json", lambda url, **k: None)
    notes = []
    assert IH.scryfall_to_uuid({1: ("sf1", "zzz")}, notes) == ({}, {})
    assert "non trovato" in notes[0]


def test_write_card_info(workdir, monkeypatch):
    monkeypatch.setattr(C, "scryfall_sets", lambda: [{"code": "lea", "released_at": "1993-08-05"},
                                                      {"code": "m10", "released_at": "2009-07-17"}])
    cards = {"u1": {"name": "Bolt", "printings": ["LEA", "M10"], "reserved": False, "edhrec": 5, "standard": True}}
    n = IH.write_card_info({1: ("sf1", "lea"), 2: ("sf2", "x")}, {"sf1": "u1"}, cards, [])
    assert n == 1
    df = pd.read_csv("data/card_info.csv")
    assert list(df.columns) == MJ.INFO_COLS
    r = df.iloc[0]
    assert r["n_printings"] == 2 and r["first_printing"] == "1993-08-05" and r["latest_printing"] == "2009-07-17"


def test_write_card_info_does_not_overwrite_with_empty(workdir):
    open("data/card_info.csv", "w").write("keep\n")
    assert IH.write_card_info({}, {}, {}, []) == 0
    assert open("data/card_info.csv").read() == "keep\n"


def test_run_end_to_end(workdir, monkeypatch):
    def ct(path, params=None, **k):
        if path == "/products/export":
            return [{"game_id": 1, "blueprint_id": 1, "properties_hash": {}},
                    {"game_id": 1, "blueprint_id": 1, "properties_hash": {"mtg_foil": True}}]
        if path == "/expansions/export":
            return [{"id": 10, "game_id": 1, "code": "lea", "name": "A"}]
        return [{"id": 1, "scryfall_id": "sf1"}]
    monkeypatch.setattr(C, "ct_get", ct)
    monkeypatch.setattr(IH, "load_mtgjson_set", lambda code: MJ_SET)
    monkeypatch.setattr(C, "scryfall_sets", lambda: [])
    monkeypatch.setattr(IH, "open_prices_stream", lambda: stream_of(PRICES))
    sent = []
    monkeypatch.setattr(C, "send_telegram", lambda t: sent.append(t))
    IH.run()
    df = pd.read_csv("data/history_mtgjson.csv")
    assert set(df.columns) == {"date", "blueprint_id", "foil", "cm_price"}
    assert len(df[df.foil == 0]) == 2 and len(df[df.foil == 1]) == 1
    assert sent and "importazione storico completata" in sent[0]


def test_run_fails_when_no_card_matches(workdir, monkeypatch):
    monkeypatch.setattr(C, "ct_get", lambda path, params=None, **k:
                        [{"game_id": 1, "blueprint_id": 1}] if path == "/products/export" else [])
    with pytest.raises(RuntimeError, match="Nessuna delle tue carte"):
        IH.run()
