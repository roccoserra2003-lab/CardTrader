"""Test di regressione: i problemi trovati nell'analisi, ora corretti, e le nuove protezioni di affidabilita'."""
import json
import os
from datetime import date

import pandas as pd
import pytest
import requests

import bot
import box_ev as B
import calendar_events as E
import common as C
import mtgjson_data as MJ
from test_integration import TODAY, World, world  # noqa: F401  (rete simulata riusata)

REAL_SEND = C.send_telegram


class Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self.ok, self._p, self.headers = status, status < 400, payload or {}, headers or {}
        self.text = "txt"

    def json(self):
        return self._p


# ================================================== 1) Play box vs altri display
@pytest.mark.parametrize("name", ["Foo Set Booster Display", "Foo Collector Booster Display", "Foo Draft Booster Display"])
def test_play_box_not_confused_with_other_displays(name):
    assert B.find_box_blueprint([{"id": 1, "name": name}], ["play", "booster"], plain_fallback=True) is None


def test_plain_fallback_not_hijacked_by_collector_display():
    bps = [{"id": 1, "name": "Foo Collector Booster Display"}, {"id": 2, "name": "Foo Booster Box"}]
    assert B.find_box_blueprint(bps, ["play", "booster"], plain_fallback=True)["id"] == 2


def test_real_play_and_collector_names_still_found():
    bps = [{"id": 1, "name": "Foo Play Booster Box"}, {"id": 2, "name": "Foo Collector Booster Box"},
           {"id": 3, "name": "Foo Play Booster Display"}, {"id": 4, "name": "Foo Collector Booster Display"}]
    assert B.find_box_blueprint(bps, ["play", "booster"])["id"] == 1
    assert B.find_box_blueprint(bps, ["collector", "booster"])["id"] == 2
    assert B.find_box_blueprint([bps[2]], ["play", "booster"])["id"] == 3       # "Play Booster Display" vale ancora


def test_has_words_is_whole_word():
    assert B.has_words("foo play booster box", ["play", "booster"])
    assert not B.has_words("foo set booster display", ["play", "booster"])


def test_packs_per_box_ignores_display_word():
    sd = {"sealedProduct": [{"name": "Foo Set Booster Display",
                             "contents": {"sealed": [{"name": "Foo Set Booster Display Pack", "count": 30}]}}]}
    assert B.packs_per_box(sd, ["play", "booster"], 36) == (36, "predefinito")


# ============================================================ 2) Telegram
def test_split_message_never_empty_or_oversize():
    for text in ("x" * 4500 + "\nfine", "a\n" * 5000, "\n\n\n", "x" * 9000, "ciao"):
        chunks = C.split_message(text)
        assert all(c.strip() and len(c) <= C.TELEGRAM_LIMIT + 1 for c in chunks)
    assert "".join(C.split_message("x" * 9000)).replace("\n", "") == "x" * 9000       # nessun carattere perso


def test_send_telegram_returns_true_when_delivered(monkeypatch, env):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200))
    assert C.send_telegram("ciao") is True


def test_send_telegram_retries_429_with_retry_after(monkeypatch, env):
    seq = [Resp(429, {"parameters": {"retry_after": 3}}), Resp(200)]
    waits = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))
    assert C.send_telegram("ciao") is True and waits == [3]


def test_send_telegram_no_retry_on_auth_error(monkeypatch, env):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or Resp(401))
    assert C.send_telegram("ciao") is False and len(calls) == 1


def test_send_telegram_network_error_does_not_leak_token(monkeypatch, env, capsys):
    def boom(*a, **k):
        raise requests.ConnectionError("failed https://api.telegram.org/bott/sendMessage")
    monkeypatch.setattr(requests, "post", boom)
    assert C.send_telegram("ciao") is False
    assert "bott/" not in capsys.readouterr().out                  # il token ('t') e' oscurato


def test_send_telegram_false_if_any_chunk_fails(monkeypatch, env):
    answers = iter([Resp(200)])                                     # il primo blocco arriva, poi Telegram rifiuta tutto
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(answers, None) or Resp(400))
    assert C.send_telegram("\n".join("riga" * 200 for _ in range(30))) is False


def test_alerts_not_logged_when_telegram_delivery_fails(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(C, "send_telegram", REAL_SEND)
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(401))
    with pytest.raises(SystemExit) as e:
        bot.run()
    assert e.value.code == bot.EXIT_CRASH
    assert not os.path.exists("data/alerts_log.csv") or pd.read_csv("data/alerts_log.csv").empty
    # alla prossima esecuzione (Telegram di nuovo ok) gli avvisi arrivano
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200))
    bot.run()
    assert len(pd.read_csv("data/alerts_log.csv")) > 0


# ============================================================ 3) Tornei
def test_tournament_parser_does_not_invent_events():
    text = "Pro Tour Zendikar Paris November 1, 2026 some unrelated text December 2, 2026"
    assert [e["name"] for e in E.parse_tournaments(text, TODAY)] == ["Pro Tour Zendikar Paris"]


def test_tournament_name_with_lowercase_words():
    ev = E.parse_tournaments("Pro Tour Edge of Eternities Tokyo November 14–16, 2026", TODAY)
    assert ev and ev[0]["name"] == "Pro Tour Edge of Eternities Tokyo"


def test_tournament_name_never_swallows_month():
    ev = E.parse_tournaments("Pro Tour Rome December 5–7, 2026", TODAY)
    assert ev[0]["name"] == "Pro Tour Rome"


def test_tournament_events_warns_when_pages_read_but_nothing_parsed(workdir, monkeypatch):
    monkeypatch.setattr(C, "web_get_json", lambda *a, **k: {"parse": {"text": "pagina senza eventi riconoscibili"}})
    notes = []
    assert E.tournament_events(TODAY, notes) == []
    assert any(n.startswith("AVVISO:") and "nessun evento" in n for n in notes)
    notes2 = []
    E.tournament_events(date(2026, 10, 27), notes2)                    # il giorno dopo: niente spam
    assert not any(n.startswith("AVVISO:") for n in notes2)


# ============================================== 4) Resilienza di CardTrader
def test_single_card_failure_does_not_lose_other_prices(world, monkeypatch):  # noqa: F811
    inv = [{"id": i, "game_id": 1, "user_id": 7, "blueprint_id": 100 + i, "name_en": f"Carta {i}", "quantity": 1,
            "price_cents": 500, "properties_hash": {}} for i in range(4)]
    orig = world.ct_get

    def ct(path, params=None, timeout=60):
        if path == "/products/export":
            return inv
        if path == "/marketplace/products" and params.get("blueprint_id") == 101:
            raise RuntimeError("HTTP 500")
        if path == "/marketplace/products" and params.get("blueprint_id", 0) >= 100:
            from conftest import listing
            return {str(params["blueprint_id"]): [listing(3.0), listing(3.2), listing(3.4)]}
        return orig(path, params, timeout)
    monkeypatch.setattr(C, "ct_get", ct)
    alerts, series, stats = bot.inventory_part(TODAY)
    assert stats["failed"] == 1 and stats["priced"] == 3 and not stats["aborted"]
    today_rows = pd.read_csv("data/prices.csv")
    assert set(today_rows[today_rows["date"] == "2026-10-26"]["blueprint_id"]) == {100, 102, 103}


def test_circuit_breaker_when_cardtrader_is_down(world, monkeypatch):  # noqa: F811
    inv = [{"id": i, "game_id": 1, "user_id": 7, "blueprint_id": 100 + i, "name_en": f"Carta {i}", "quantity": 1,
            "price_cents": 500, "properties_hash": {}} for i in range(50)]
    calls = []
    orig = world.ct_get

    def ct(path, params=None, timeout=60):
        if path == "/products/export":
            return inv
        if path == "/marketplace/products" and params.get("blueprint_id", 0) >= 100:
            calls.append(1)
            raise RuntimeError("HTTP 503")
        return orig(path, params, timeout)
    monkeypatch.setattr(C, "ct_get", ct)
    with pytest.raises(SystemExit) as e:
        bot.run()
    assert len(calls) == bot.MAX_CONSECUTIVE_FAILURES               # si ferma subito invece di provare 50 volte
    assert e.value.code == bot.EXIT_PARTIAL
    assert "non risponde" in world.sent[0]
    assert "EV BOX" in world.sent[0]                               # le altre parti del bot funzionano comunque


def test_partial_rerun_keeps_todays_prices_for_other_cards(world, monkeypatch):  # noqa: F811
    bot.run()
    df = pd.read_csv("data/prices.csv")
    df = pd.concat([df, df[df["date"] == "2026-10-26"].assign(blueprint_id=777, name="Altra carta")])
    df.to_csv("data/prices.csv", index=False)
    bot.run()                                                       # rilancio: aggiorna solo le carte in inventario
    again = pd.read_csv("data/prices.csv")
    today = again[again["date"] == "2026-10-26"]
    assert set(today["blueprint_id"]) == {13, 777}                  # la riga della carta non rilanciata resta
    assert not today.duplicated(["blueprint_id", "foil", "language"]).any()


def test_request_honors_retry_after_and_no_final_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))
    monkeypatch.setattr(requests, "request", lambda *a, **k: Resp(429, headers={"Retry-After": "7"}))
    with pytest.raises(RuntimeError):
        C._request("GET", "http://x", retries=3)
    assert waits == [7, 7]                                          # 3 tentativi = 2 attese, nessuna dopo l'ultimo


# ========================================================= 5) Scritture atomiche
def test_save_csv_and_json_are_atomic(workdir, monkeypatch):
    C.save_csv(pd.DataFrame({"a": [1]}), "data/x.csv")
    good = open("data/x.csv").read()

    class Boom(pd.DataFrame):
        def to_csv(self, *a, **k):
            a[0].write("a\n9\n")                                    # scrive meta' e poi va in crash
            raise OSError("disco pieno")
    with pytest.raises(OSError):
        C.save_csv(Boom({"a": [2]}), "data/x.csv")
    assert open("data/x.csv").read() == good                        # il file originale e' intatto
    assert os.listdir("data") == ["x.csv"]                          # nessun file temporaneo rimasto
    C.save_json("data/y.json", {"k": 1})
    assert json.load(open("data/y.json")) == {"k": 1}


def test_env_missing_has_clear_message(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(RuntimeError, match="NOPE"):
        C.env("NOPE")
