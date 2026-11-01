import json
from datetime import date

import pandas as pd
import pytest

import calendar_events as E
import common as C

TODAY = date(2026, 10, 26)


# ------------------------------------------------------------------ tornei
def test_parse_tournaments_basic_ranges():
    text = ("Pro Tour Aetherdrift Tokyo, Japan November 14–16, 2026 "
            "Regional Championships 2026 Madrid March 28 – April 2, 2027 "
            "Magic Limited Championship Chicago December 5, 2026")
    ev = E.parse_tournaments(text, TODAY)
    byname = {e["name"]: e for e in ev}
    assert byname["Pro Tour Aetherdrift Tokyo"]["start"] == "2026-11-14"
    assert byname["Pro Tour Aetherdrift Tokyo"]["end"] == "2026-11-16"
    assert byname["Regional Championships 2026"]["start"] == "2027-03-28"
    assert byname["Regional Championships 2026"]["end"] == "2027-04-02"    # intervallo a cavallo di mesi
    assert byname["Magic Limited Championship"]["start"] == "2026-12-05"
    assert all(e["type"] == "tournament" and e["source"] == "wiki" for e in ev)


def test_parse_tournaments_ignores_citation_dates_and_out_of_range():
    text = ("Pro Tour Foo Bar (published March 3, 2026) "
            "Pro Tour Old Event January 5, 2020 "
            "Pro Tour Far Away June 1, 2040 "
            "Pro Tour Bad Date February 31, 2026")
    assert E.parse_tournaments(text, TODAY) == []


def test_parse_tournaments_dedup():
    text = "Pro Tour Zendikar Paris November 1, 2026 Pro Tour Zendikar Paris November 1, 2026"
    assert len(E.parse_tournaments(text, TODAY)) == 1


def test_parse_tournaments_end_before_start_skipped():
    assert E.parse_tournaments("Pro Tour Oops Rome November 20–10, 2026", TODAY) == []


def test_wiki_text_strips_html_and_references():
    data = {"parse": {"text": "<p>Pro Tour <b>X</b> &amp; Y</p><script>bad()</script><h2>References</h2>noise"}}
    t = E._wiki_text(data)
    assert "Pro Tour X & Y" in t and "bad()" not in t and "noise" not in t
    assert E._wiki_text(None) == "" and E._wiki_text({}) == ""
    assert "hello" in E._wiki_text({"parse": {"text": {"*": "hello"}}})


def test_tournament_events_all_fail_warns_once_per_period(workdir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(C, "web_get_json", boom)
    notes = []
    assert E.tournament_events(TODAY, notes) == []
    assert any(n.startswith("AVVISO:") for n in notes)
    notes2 = []
    E.tournament_events(date(2026, 10, 27), notes2)                       # il giorno dopo: niente nuovo AVVISO
    assert not any(n.startswith("AVVISO:") for n in notes2)
    notes3 = []
    E.tournament_events(date(2026, 11, 20), notes3)                       # dopo 14+ giorni: riavvisa
    assert any(n.startswith("AVVISO:") for n in notes3)


def test_tournament_events_success(workdir, monkeypatch):
    page = {"parse": {"text": "Pro Tour Test City Paris November 14–16, 2026"}}
    monkeypatch.setattr(C, "web_get_json", lambda *a, **k: page)
    ev = E.tournament_events(TODAY, [])
    assert ev and ev[0]["name"] == "Pro Tour Test City Paris"


# ------------------------------------------------------------ set e rotazione
def sc(code, name, released, stype="expansion", **kw):
    return {"code": code, "name": name, "released_at": released, "set_type": stype, **kw}


def test_set_events_release_br_window_and_rotation(monkeypatch):
    monkeypatch.setattr(C, "scryfall_sets", lambda: [
        sc("a", "Alpha", "2026-09-26"),
        sc("b", "Beta", "2027-02-06"),
        sc("c", "Gamma", "2027-06-05"),
        sc("m", "Masters", "2026-12-01", "masters"),
        sc("t", "Token", "2026-12-01", "token"),
        sc("d", "Digital", "2026-12-01", digital=True),
    ])
    ev = pd.DataFrame(E.set_events(TODAY))
    assert set(ev["type"]) == {"release", "release_other", "br_window", "rotation"}
    br = ev[(ev["type"] == "br_window") & ev["name"].str.contains("Alpha")].iloc[0]
    assert br["start"] == "2026-10-10" and br["end"] == "2026-10-31" and br["approx"]
    rot = ev[ev["type"] == "rotation"]
    assert len(rot) == 1 and rot.iloc[0]["start"] == "2027-02-06" and "Beta" in rot.iloc[0]["name"]
    assert not ev["name"].str.contains("Token|Digital").any()


def test_set_events_rotation_estimated_when_no_set(monkeypatch):
    monkeypatch.setattr(C, "scryfall_sets", lambda: [sc("a", "Alpha", "2026-09-26")])
    rot = [e for e in E.set_events(TODAY) if e["type"] == "rotation"]
    assert rot and rot[0]["start"] == "2027-02-01" and "stimata" in rot[0]["name"]


def test_set_events_window_limits(monkeypatch):
    monkeypatch.setattr(C, "scryfall_sets", lambda: [sc("o", "TooOld", "2024-01-01"), sc("f", "TooFar", "2029-01-01")])
    assert [e for e in E.set_events(TODAY) if e["type"].startswith("release")] == []


# ------------------------------------------------------------------- ban reali
def test_fetch_banned_pagination(monkeypatch):
    pages = [
        {"data": [{"name": "B"}, {"name": "A"}], "has_more": True, "next_page": "http://next"},
        {"data": [{"name": "C"}], "has_more": False},
    ]
    seen = []
    monkeypatch.setattr(C, "web_get_json", lambda url, params=None, **k: seen.append(url) or pages.pop(0))
    assert E.fetch_banned("modern") == ["A", "B", "C"]
    assert seen[1] == "http://next"


def test_fetch_banned_404(monkeypatch):
    monkeypatch.setattr(C, "web_get_json", lambda *a, **k: None)
    assert E.fetch_banned("modern") == []


def patch_bans(monkeypatch, mapping):
    def fb(fmt):
        v = mapping.get(fmt, [])
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(E, "fetch_banned", fb)


def test_detect_ban_changes_first_run_only_saves_baseline(workdir, monkeypatch):
    patch_bans(monkeypatch, {"modern": ["A", "B"]})
    assert E.detect_ban_changes([]) == []
    saved = json.load(open("data/bans_snapshot.json"))
    assert saved["modern"] == ["A", "B"] and set(saved) == set(E.BAN_FORMATS)


def test_detect_ban_changes_diff(workdir, monkeypatch):
    patch_bans(monkeypatch, {"modern": ["A", "B"], "pauper": ["P"]})
    E.detect_ban_changes([])
    patch_bans(monkeypatch, {"modern": ["A", "C"], "pauper": ["P"]})
    ch = E.detect_ban_changes([])
    assert ch == [{"format": "modern", "banned": ["C"], "unbanned": ["B"]}]


def test_detect_ban_changes_keeps_yesterday_on_errors_or_suspicious_empty(workdir, monkeypatch):
    old = {fmt: [f"{fmt}{i}" for i in range(10)] for fmt in E.BAN_FORMATS}
    C.save_json("data/bans_snapshot.json", old)
    patch_bans(monkeypatch, {"modern": RuntimeError("api giu'"), "legacy": [], "standard": old["standard"],
                             "pioneer": old["pioneer"], "commander": old["commander"], "pauper": old["pauper"]})
    notes = []
    assert E.detect_ban_changes(notes) == []
    assert any("modern" in n for n in notes)
    assert C.load_json("data/bans_snapshot.json") == old        # nessun dato perso


def test_detect_ban_changes_anomaly_suppressed(workdir, monkeypatch):
    C.save_json("data/bans_snapshot.json", {"modern": []})
    patch_bans(monkeypatch, {"modern": [f"c{i}" for i in range(70)]})
    notes = []
    assert E.detect_ban_changes(notes) == []
    assert any("anomalia" in n for n in notes)


# ---------------------------------------------------------------- collect_events
def test_collect_events_accumulates_wiki_and_ban_only(workdir, monkeypatch):
    monkeypatch.setattr(E, "set_events", lambda today: [E._event("release", "Uscita A", TODAY, TODAY, False, "scryfall")])
    monkeypatch.setattr(E, "tournament_events", lambda today, notes: [E._event("tournament", "PT X", TODAY, TODAY, False, "wiki")])
    ban = [{"format": "modern", "banned": ["A"], "unbanned": []}]
    df = E.collect_events(TODAY, [], ban)
    assert set(df["type"]) == {"release", "tournament", "ban"}
    # il giorno dopo: set cambiati, wiki non raggiungibile -> tornei e ban storici restano, set vecchi spariscono
    monkeypatch.setattr(E, "set_events", lambda today: [E._event("release", "Uscita B", TODAY, TODAY, False, "scryfall")])
    monkeypatch.setattr(E, "tournament_events", lambda today, notes: [])
    df = E.collect_events(date(2026, 10, 27), [], [])
    assert sorted(df["name"]) == ["PT X", "Uscita B", "modern: 1 ban, 0 unban"]
    assert not df.duplicated(subset=["type", "name", "start"]).any()
    assert pd.read_csv("data/events.csv").shape[0] == 3


def test_collect_events_survives_component_failures(workdir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(E, "set_events", boom)
    monkeypatch.setattr(E, "tournament_events", boom)
    notes = []
    df = E.collect_events(TODAY, notes, [])
    assert df.empty and len(notes) == 2


# ------------------------------------------------------- effetto e avvisi
def mk_events(rows):
    return pd.DataFrame(rows, columns=E.EVENT_COLS)


def test_event_effects_median_change():
    idx = pd.date_range("2026-01-01", "2026-10-26")
    s = pd.Series(10.0, index=idx)
    s[s.index >= "2026-05-10"] = 12.0                         # +20% dopo l'evento del 10 maggio
    events = mk_events([["release", f"R{i}", "2026-05-%02d" % (8 + i), "2026-05-%02d" % (8 + i), "False", "scryfall"]
                        for i in range(3)])
    eff = E.event_effects(events, {"k": s}, TODAY)
    assert eff["release"]["n_events"] == 3 and eff["release"]["median"] > 0


def test_event_effects_ignores_future_and_prehistory():
    idx = pd.date_range("2026-10-01", "2026-10-26")
    s = pd.Series(10.0, index=idx)
    events = mk_events([["release", "Old", "2026-01-01", "2026-01-01", "False", "x"],        # prima dello storico
                        ["release", "Soon", "2026-10-25", "2026-10-25", "False", "x"]])     # finestra non conclusa
    assert E.event_effects(events, {"k": s}, TODAY) == {}
    assert E.event_effects(mk_events([]), {"k": s}, TODAY) == {}
    assert E.event_effects(events, {}, TODAY) == {}


def test_upcoming_texts_selection_and_wording():
    rows = [
        ["release", "Uscita Soon", "2026-10-30", "2026-10-30", "False", "scryfall"],
        ["br_window", "Finestra B&R", "2026-10-20", "2026-11-05", "True", "calc"],
        ["release", "Uscita Today", "2026-10-26", "2026-10-26", "False", "scryfall"],
        ["release", "Uscita Far", "2027-03-01", "2027-03-01", "False", "scryfall"],
        ["release", "Uscita Past", "2026-09-01", "2026-09-01", "False", "scryfall"],
        ["ban", "modern: 1 ban, 0 unban", "2026-10-26", "2026-10-26", "False", "ban"],
    ]
    out = dict(E.upcoming_texts(mk_events(rows), {}, TODAY))
    texts = " ".join(out.values())
    assert "tra 4 giorni (30/10)" in texts and "oggi" in texts and "in corso fino al 05/11" in texts and "[stima]" in texts
    assert "Far" not in texts and "Past" not in texts and "ban" not in texts
    assert "storico ancora insufficiente" in texts
    assert "release:Uscita Soon:2026-10-30" in out


def test_upcoming_texts_with_effect():
    rows = [["release", "Uscita Soon", "2026-10-30", "2026-10-30", "False", "scryfall"]]
    eff = {"release": {"n_events": 5, "n_obs": 100, "median": -3.2}}
    ((key, text),) = E.upcoming_texts(mk_events(rows), eff, TODAY)
    assert "-3.2%" in text and "5 eventi" in text
    eff["release"]["n_events"] = 1
    assert "insufficiente" in E.upcoming_texts(mk_events(rows), eff, TODAY)[0][1]


def test_upcoming_texts_empty_inputs():
    assert E.upcoming_texts(mk_events([]), {}, TODAY) == []
    only_ban = mk_events([["ban", "x", "2026-10-26", "2026-10-26", "False", "ban"]])
    assert E.upcoming_texts(only_ban, {}, TODAY) == []         # tutti filtrati: non deve crashare
