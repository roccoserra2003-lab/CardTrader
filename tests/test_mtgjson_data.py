import gzip
import io
import json
import os
import pathlib
from datetime import date

import mtgjson_data as MJ

TODAY = date(2026, 10, 26)


# --------------------------------------------------------------- risk_labels
def test_risk_labels_empty():
    assert MJ.risk_labels(None, TODAY) == []
    assert MJ.risk_labels({}, TODAY) == []


def test_risk_labels_reserved_and_edhrec_and_standard():
    info = {"is_reserved": "True", "n_printings": "1", "latest_printing": "1995-01-01",
            "edhrec_rank": "120", "standard": "True"}
    labels = MJ.risk_labels(info, TODAY)
    assert "Reserved List (non ristampabile)" in labels
    assert "EDHREC #120" in labels
    assert "legale in Standard" in labels


def test_risk_labels_recent_reprint_and_upcoming():
    recent = {"n_printings": "3", "latest_printing": "2026-06-01"}
    assert any("ristampata di recente" in l for l in MJ.risk_labels(recent, TODAY))
    upcoming = {"n_printings": "2", "latest_printing": "2026-12-01"}
    assert any("ristampa in arrivo" in l for l in MJ.risk_labels(upcoming, TODAY))


def test_risk_labels_many_printings_old():
    info = {"n_printings": "24", "latest_printing": "2022-07-07"}
    assert MJ.risk_labels(info, TODAY) == ["ristampata spesso (24 stampe)"]
    # poche stampe e vecchie: nessuna etichetta
    assert MJ.risk_labels({"n_printings": "2", "latest_printing": "2020-01-01"}, TODAY) == []


def test_risk_labels_edhrec_threshold():
    assert MJ.risk_labels({"edhrec_rank": "1500"}, TODAY) == ["EDHREC #1500"]
    assert MJ.risk_labels({"edhrec_rank": "1501"}, TODAY) == []
    assert MJ.risk_labels({"edhrec_rank": ""}, TODAY) == []


def test_risk_labels_garbage_values_do_not_crash():
    info = {"n_printings": "abc", "latest_printing": "not-a-date", "edhrec_rank": "x"}
    assert MJ.risk_labels(info, TODAY) == []
    info = {"n_printings": "3", "latest_printing": "not-a-date"}
    assert MJ.risk_labels(info, TODAY) == []


# ------------------------------------------------------------- load_card_info
def test_load_card_info_missing(workdir):
    assert MJ.load_card_info() == {}


def test_load_card_info_real_file(monkeypatch):
    root = pathlib.Path(__file__).resolve().parent.parent
    monkeypatch.chdir(root)
    info = MJ.load_card_info()
    assert len(info) > 100
    k, v = next(iter(info.items()))
    assert isinstance(k, int) and set(MJ.INFO_COLS) <= set(v)
    for rec in info.values():                       # nessuna etichetta deve far crashare
        MJ.risk_labels(rec, TODAY)


# ------------------------------------------------------ fetch_today_cardmarket
def _gz(obj):
    return io.BytesIO(gzip.compress(json.dumps(obj).encode()))


def _opener(obj):
    return lambda url: gzip.GzipFile(fileobj=_gz(obj))


def test_fetch_today_cardmarket_parses_latest_prices():
    data = {"meta": {}, "data": {
        "u1": {"paper": {"cardmarket": {"currency": "EUR", "retail": {
            "normal": {"2026-10-24": 1.0, "2026-10-26": 2.5}, "foil": {"2026-10-26": 9.0}}}}},
        "u2": {"paper": {"cardmarket": {"currency": "USD", "retail": {"normal": {"2026-10-26": 5}}}}},
        "u3": {"paper": {"tcgplayer": {"retail": {"normal": {"2026-10-26": 7}}}}},
        "u4": {"paper": {"cardmarket": {"currency": "EUR", "retail": {"normal": {"2026-10-26": 3}}}}},
    }}
    notes = []
    out = MJ.fetch_today_cardmarket({"u1", "u2", "u3"}, notes, opener=_opener(data))
    assert out == {"u1": {"normal": 2.5, "foil": 9.0}}       # u2 in USD, u3 senza Cardmarket, u4 non richiesto
    assert any("1 carte su 3" in n for n in notes)


def test_fetch_today_cardmarket_failure_is_graceful():
    def bad(url):
        raise OSError("rete assente")
    notes = []
    assert MJ.fetch_today_cardmarket({"u1"}, notes, opener=bad) == {}
    assert notes and "non disponibile" in notes[0]


def test_fetch_today_cardmarket_null_entries():
    data = {"data": {"u1": None, "u2": {"paper": None}}}
    assert MJ.fetch_today_cardmarket({"u1", "u2"}, [], opener=_opener(data)) == {}
