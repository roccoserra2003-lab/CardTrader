"""Esecuzione completa di bot.run() contro una rete simulata."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import bot
import box_ev
import calendar_events as E
import common as C
import mtgjson_data as MJ
from conftest import listing

TODAY = date(2026, 10, 26)


class World:
    """Piccolo universo: 1 set recente con 3 carte e un Play Booster Box; 1 carta nell'inventario con prezzo in salita."""

    def __init__(self):
        self.sent = []
        self.telegram_ok = True
        self.fail = set()                      # nomi di endpoint da far fallire
        self.box_price = 50.0

    # --- CardTrader
    def ct_get(self, path, params=None, timeout=60):
        if path in self.fail:
            raise RuntimeError(f"{path} giu'")
        params = params or {}
        if path == "/products/export":
            return [{"id": 1, "game_id": 1, "user_id": 7, "blueprint_id": 13, "name_en": "Rare Uno", "quantity": 2,
                     "price_cents": 1500, "properties_hash": {"mtg_language": "en", "condition": "Near Mint"}}]
        if path == "/expansions":
            return [{"id": 1, "game_id": 1, "code": "tst", "name": "Test Set"}]
        if path == "/blueprints/export":
            return [{"id": 11, "name": "Comune A", "scryfall_id": "sc1"},
                    {"id": 12, "name": "Comune B", "scryfall_id": "sc2"},
                    {"id": 13, "name": "Rare Uno", "scryfall_id": "sr1"},
                    {"id": 99, "name": "Test Set Play Booster Box"}]
        if path == "/marketplace/products":
            if "expansion_id" in params:
                return {"11": [listing(p) for p in (1.0, 1.1, 1.2)],
                        "12": [listing(p) for p in (1.0, 1.1, 1.2)],
                        "13": [listing(p) for p in (14, 14.5, 15, 15.5)]}
            if params.get("blueprint_id") == 99:
                return {"99": [listing(self.box_price), listing(self.box_price + 5), listing(self.box_price + 8)]}
            if params.get("blueprint_id") == 13:
                return {"13": [listing(p, user=u) for p, u in ((15, 3), (15.5, 4), (16, 5), (5, 7))]}
        return {}

    # --- web pubblici
    def web_get_json(self, url, params=None, timeout=60, allow_404=False):
        if "api.scryfall.com/sets" in url:
            return {"data": [{"code": "tst", "name": "Test Set", "released_at": "2026-09-26", "set_type": "expansion"},
                             {"code": "nxt", "name": "Next Set", "released_at": "2026-11-06", "set_type": "expansion"}]}
        if "cards/search" in url:
            return {"data": [{"name": "Some Card"}], "has_more": False}
        if "mtg.wiki" in url:
            return {"parse": {"text": "Pro Tour Testland Rome November 14–16, 2026"}}
        if "mtgjson.com/api/v5/TST.json" in url:
            return {"data": {
                "cards": [{"uuid": "c1", "name": "Comune A", "identifiers": {"scryfallId": "sc1"}},
                          {"uuid": "c2", "name": "Comune B", "identifiers": {"scryfallId": "sc2"}},
                          {"uuid": "r1", "name": "Rare Uno", "identifiers": {"scryfallId": "sr1"}}],
                "booster": {"play": {"sheets": {"common": {"cards": {"c1": 1, "c2": 1}}, "rare": {"cards": {"r1": 1}}},
                                     "boosters": [{"contents": {"common": 2, "rare": 1}, "weight": 1}]}},
                "sealedProduct": [{"name": "Test Set Play Booster Box",
                                   "contents": {"sealed": [{"name": "Test Set Play Booster Pack", "count": 36}]}}]}}
        return None

    def telegram(self, text):
        if self.telegram_ok:
            self.sent.append(text)
        return self.telegram_ok


@pytest.fixture
def world(workdir, monkeypatch, env):
    w = World()
    monkeypatch.setattr(C, "ct_get", w.ct_get)
    monkeypatch.setattr(C, "web_get_json", w.web_get_json)
    monkeypatch.setattr(C, "send_telegram", w.telegram)
    monkeypatch.setattr(MJ, "fetch_today_cardmarket", lambda wanted, notes, **k: {})
    C._SETS_CACHE.clear()
    monkeypatch.setattr(bot, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 10, 26, 6, tzinfo=timezone.utc))}))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    # storico: 30 giorni di prezzo stabile a 10 EUR, oggi il mercato e' a ~15
    days = pd.date_range(end=TODAY - pd.Timedelta(days=1), periods=30).strftime("%Y-%m-%d")
    pd.DataFrame({"date": days, "blueprint_id": 13, "foil": 0, "language": "en", "name": "Rare Uno",
                  "market": 10.0, "min": 10.0, "n_listings": 5}).to_csv("data/prices.csv", index=False)
    return w


def test_full_run_sends_sell_box_and_events(world):
    bot.run()
    assert len(world.sent) == 1
    msg = world.sent[0]
    assert "MTG Bot - 2026-10-26" in msg
    assert "VENDI ORA" in msg and "Rare Uno (EN) x2" in msg              # +50% rispetto alla mediana
    assert "EV BOX SOPRA IL PREZZO" in msg and "Test Set - Play Booster Box" in msg
    assert "EVENTI IN ARRIVO" in msg and "Uscita Next Set" in msg
    assert "ERRORI" not in msg
    # la mia offerta (5 EUR, user 7) non deve influenzare il prezzo di riferimento (mediana 15.5)
    assert "ora 15.50 EUR" in msg


def test_full_run_persists_files(world):
    bot.run()
    prices = pd.read_csv("data/prices.csv")
    assert (prices["date"] == "2026-10-26").sum() == 1 and prices["date"].nunique() == 31
    box = pd.read_csv("data/box_ev.csv")
    assert box.iloc[0]["set_code"] == "tst" and box.iloc[0]["ratio"] > 1.1
    assert pd.read_csv("data/events.csv").shape[0] > 0
    alerts = pd.read_csv("data/alerts_log.csv")
    assert {"sell", "box", "event"} <= set(alerts["type"])
    assert pd.read_json("data/bans_snapshot.json").shape[0] > 0


def test_second_run_same_day_is_deduplicated(world):
    bot.run()
    world.sent.clear()
    bot.run()
    assert world.sent == []                                              # cooldown: niente spam
    prices = pd.read_csv("data/prices.csv")
    assert (prices["date"] == "2026-10-26").sum() == 1                   # il rilancio sostituisce i dati di oggi


def test_manual_run_adds_status_section(world, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    bot.run()
    assert "STATO" in world.sent[0] and "Box valutati: 1" in world.sent[0]


def test_failure_in_inventory_reports_error_and_exits_1(world):
    world.fail.add("/products/export")
    with pytest.raises(SystemExit) as e:
        bot.run()
    assert e.value.code == bot.EXIT_PARTIAL                              # errori gia' comunicati su Telegram
    assert "ERRORI" in world.sent[0] and "Inventario" in world.sent[0]
    assert "EV BOX" in world.sent[0]                                     # il resto del bot continua a funzionare


def test_failure_in_box_ev_reports_error_but_keeps_other_parts(world):
    world.fail.add("/expansions")
    with pytest.raises(SystemExit):
        bot.run()
    assert "VENDI ORA" in world.sent[0] and "EV box" in world.sent[0]


def test_no_alert_for_unreliable_pack_count(world, monkeypatch):
    orig = world.web_get_json

    def no_sealed(url, **k):
        r = orig(url, **k)
        if r and "mtgjson.com/api/v5/TST" in url:
            r["data"]["sealedProduct"] = []
        return r
    monkeypatch.setattr(C, "web_get_json", no_sealed)
    bot.run()
    assert "EV BOX SOPRA" not in world.sent[0]


def test_expensive_box_is_not_flagged(world):
    world.box_price = 900.0
    bot.run()
    assert "EV BOX SOPRA" not in world.sent[0]


def test_empty_inventory(world, monkeypatch):
    orig = world.ct_get
    monkeypatch.setattr(C, "ct_get", lambda path, params=None, timeout=60: [] if path == "/products/export" else orig(path, params, timeout))
    bot.run()
    assert "VENDI ORA" not in world.sent[0]
