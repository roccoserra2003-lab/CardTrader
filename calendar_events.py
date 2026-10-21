"""
Calendario eventi MTG costruito in automatico:
- uscite dei set (Scryfall)
- rotazione dello Standard (regola applicata alle date dei set)
- finestre stimate degli annunci Banned & Restricted (dopo ogni set)
- ban/unban reali (confronto giornaliero delle carte bandite)
- tornei (lettura best-effort di una wiki; se fallisce il bot continua)
Inoltre misura come si sono mossi in passato i tuoi prezzi attorno a ciascun tipo di evento.
"""
import html as html_lib
import re
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

import common as C

# =====================================================================
# IMPOSTAZIONI
# =====================================================================
ENABLE_TOURNAMENT_FETCH = True       # metti False per disattivare la lettura dei tornei
WIKI_API = "https://mtg.wiki/api.php"
BR_WINDOW_FROM_DAYS = 14             # annunci B&R: da 2 settimane...
BR_WINDOW_TO_DAYS = 35               # ...a 5 settimane dopo l'uscita di un set
BAN_FORMATS = ["standard", "pioneer", "modern", "legacy", "commander", "pauper"]
UPCOMING_DAYS = 14                   # avvisa degli eventi dei prossimi 14 giorni
EFFECT_DAYS = 14                     # finestra usata per misurare l'effetto storico
MIN_EVENTS_FOR_EFFECT = 3            # eventi minimi per fidarsi dell'effetto storico
MIN_OBS_FOR_EFFECT = 20              # osservazioni minime per fidarsi dell'effetto storico
TOURNAMENT_WARNING_EVERY_DAYS = 14   # ogni quanto avvisarti se la lettura dei tornei fallisce
# =====================================================================

EVENTS_FILE = "data/events.csv"
BANS_FILE = "data/bans_snapshot.json"
STATE_FILE = "data/state.json"
EVENT_COLS = ["type", "name", "start", "end", "approx", "source"]
PREMIER_TYPES = {"expansion", "core"}
OTHER_RELEASE_TYPES = {"masters", "draft_innovation", "commander", "eternal"}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_RE = "|".join(MONTHS)
DATE_RE = re.compile(
    rf"({MONTH_RE})\s+(\d{{1,2}})(?:\s*[–\-]\s*(?:({MONTH_RE})\s+)?(\d{{1,2}}))?,?\s+(\d{{4}})"
)
EVENT_NAME_RE = re.compile(
    r"(Pro Tour [A-Z][A-Za-z']+(?: [A-Z][A-Za-z']+){0,3}"
    r"|Arena Championship \d+|World Championship \d+"
    r"|Regional Championships? \d+|Magic Limited Championship"
    r"|MagicCon:? [A-Z][a-z]+)"
)


def _event(etype, name, start, end, approx, source):
    return {"type": etype, "name": name, "start": start.isoformat(), "end": end.isoformat(),
            "approx": bool(approx), "source": source}


# ------------------------------------------------------- Set e rotazione
def set_events(today):
    events, premier_all = [], []
    lo, hi = today - timedelta(days=450), today + timedelta(days=365)
    for s in C.scryfall_sets():
        if s.get("digital") or s.get("parent_set_code") or not s.get("released_at"):
            continue
        stype = s.get("set_type")
        if stype not in PREMIER_TYPES and stype not in OTHER_RELEASE_TYPES:
            continue
        released = date.fromisoformat(s["released_at"])
        is_premier = stype in PREMIER_TYPES
        if is_premier:
            premier_all.append((released, s["name"]))
        if released < lo or released > hi:
            continue
        events.append(_event("release" if is_premier else "release_other",
                             f"Uscita {s['name']}", released, released, False, "scryfall"))
        if is_premier:
            events.append(_event(
                "br_window", f"Finestra annunci B&R dopo {s['name']}",
                released + timedelta(days=BR_WINDOW_FROM_DAYS),
                released + timedelta(days=BR_WINDOW_TO_DAYS), True, "calc"))

    # Dal 2027 la rotazione avviene con il primo set premier dell'anno
    for year in range(max(2027, today.year), today.year + 2):
        in_year = sorted(p for p in premier_all if p[0].year == year)
        if in_year:
            rdate, rname = in_year[0]
            events.append(_event(
                "rotation", f"Rotazione Standard con {rname} (escono i set del {year - 3})",
                rdate, rdate, True, "calc"))
        else:
            d = date(year, 2, 1)
            events.append(_event(
                "rotation", f"Rotazione Standard {year} (data stimata, set non ancora annunciato)",
                d, d, True, "calc"))
    return events


# ---------------------------------------------------------------- Tornei
def _wiki_text(data):
    if not data or "parse" not in data:
        return ""
    t = data["parse"].get("text", "")
    if isinstance(t, dict):
        t = t.get("*", "")
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    t = re.sub(r"\s+", " ", t)
    return re.split(r"\bReferences\b", t)[0]


def parse_tournaments(text, today):
    events, seen = [], set()
    lo, hi = today - timedelta(days=120), today + timedelta(days=400)
    for m in DATE_RE.finditer(text):
        if text[m.end():m.end() + 1] == ")":      # data di pubblicazione di un articolo citato
            continue
        window = text[max(0, m.start() - 160):m.start()]
        names = EVENT_NAME_RE.findall(window)
        if not names:
            continue
        month1, day1, month2, day2, year = m.groups()
        month2 = month2 or month1
        day2 = day2 or day1
        try:
            start = date(int(year), MONTHS.index(month1) + 1, int(day1))
            end = date(int(year), MONTHS.index(month2) + 1, int(day2))
        except ValueError:
            continue
        if end < start or start < lo or start > hi:
            continue
        name = names[-1].strip()
        if (name, start) in seen:
            continue
        seen.add((name, start))
        events.append(_event("tournament", name, start, end, False, "wiki"))
    return events


def tournament_events(today, notes):
    events, ok = [], 0
    start_year = today.year if today.month >= 9 else today.year - 1
    for y in (start_year - 1, start_year, start_year + 1):
        title = f"{y}–{str(y + 1)[2:]} Premier Play Season"
        try:
            data = C.web_get_json(WIKI_API, params={
                "action": "parse", "page": title, "prop": "text",
                "format": "json", "formatversion": 2, "redirects": 1})
        except Exception as e:
            notes.append(f"Lettura tornei ({title}) non riuscita: {e}")
            continue
        time.sleep(1)
        text = _wiki_text(data)
        if not text:
            continue
        ok += 1
        events.extend(parse_tournaments(text, today))
    if ok == 0:
        notes.append("Lettura tornei non riuscita: nessuna pagina del calendario e' stata letta.")
        state = C.load_json(STATE_FILE, {})
        last = state.get("tournament_warning")
        due = True
        if last:
            due = (today - date.fromisoformat(last)).days >= TOURNAMENT_WARNING_EVERY_DAYS
        if due:
            notes.append("AVVISO: non riesco a leggere il calendario dei tornei (il resto del bot funziona). "
                         "Se persiste, il sito potrebbe aver cambiato struttura.")
            state["tournament_warning"] = today.isoformat()
            C.save_json(STATE_FILE, state)
    return events


# ------------------------------------------------------------- Ban reali
def fetch_banned(fmt):
    names, url, params = set(), "https://api.scryfall.com/cards/search", {"q": f"banned:{fmt}", "unique": "cards"}
    for _ in range(12):
        data = C.web_get_json(url, params=params, allow_404=True)
        time.sleep(0.15)
        if not data:
            break
        names.update(c["name"] for c in data.get("data", []))
        if data.get("has_more") and data.get("next_page"):
            url, params = data["next_page"], None
        else:
            break
    return sorted(names)


def detect_ban_changes(notes):
    """Confronta le carte bandite di oggi con quelle di ieri. Al primo avvio salva solo la base."""
    old = C.load_json(BANS_FILE, None)
    new = {}
    for fmt in BAN_FORMATS:
        try:
            names = fetch_banned(fmt)
        except Exception as e:
            notes.append(f"Lettura ban {fmt} non riuscita: {e}")
            names = None
        if old and fmt in old and (names is None or (len(names) == 0 and len(old[fmt]) >= 5)):
            names = old[fmt]            # dato sospetto o mancante: tengo quello di ieri
        if names is not None:
            new[fmt] = names
    changes = []
    if old:
        for fmt, names in new.items():
            if fmt not in old:
                continue
            added = sorted(set(names) - set(old[fmt]))
            removed = sorted(set(old[fmt]) - set(names))
            if added or removed:
                changes.append({"format": fmt, "banned": added, "unbanned": removed})
    total = sum(len(c["banned"]) + len(c["unbanned"]) for c in changes)
    if total > 60:
        notes.append(f"Ban: {total} variazioni in un giorno, probabile anomalia dei dati: non notifico.")
        changes = []
    C.save_json(BANS_FILE, new)
    return changes


# --------------------------------------------------------- Database eventi
def collect_events(today, notes, ban_changes=None):
    new_events = []
    try:
        new_events += set_events(today)
    except Exception as e:
        notes.append(f"Lettura set non riuscita: {e}")
    if ENABLE_TOURNAMENT_FETCH:
        try:
            new_events += tournament_events(today, notes)
        except Exception as e:
            notes.append(f"Lettura tornei non riuscita: {e}")
    for c in ban_changes or []:
        new_events.append(_event(
            "ban", f"{c['format']}: {len(c['banned'])} ban, {len(c['unbanned'])} unban",
            today, today, False, "ban"))

    old = C.load_csv(EVENTS_FILE, EVENT_COLS)
    keep = old[old["source"].isin(["wiki", "ban"])]     # tornei e ban si accumulano nel tempo
    fresh = pd.DataFrame(new_events, columns=EVENT_COLS)
    frames = [f for f in (keep, fresh) if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=EVENT_COLS)
    df["approx"] = df["approx"].astype(str)
    df = df.drop_duplicates(subset=["type", "name", "start"]).reset_index(drop=True)
    df.to_csv(EVENTS_FILE, index=False)
    return df


# ------------------------------------------------ Effetto storico e avvisi
def event_effects(events, series_by_key, today):
    """Per ogni tipo di evento: variazione mediana dei tuoi prezzi nei giorni successivi."""
    out = {}
    if events.empty or not series_by_key:
        return out
    series_list = [s for s in series_by_key.values() if len(s) >= 2]
    if not series_list:
        return out
    global_min = min(s.index.min() for s in series_list)
    today_ts = pd.Timestamp(today)
    post = pd.Timedelta(days=EFFECT_DAYS)
    for etype, grp in events.groupby("type"):
        changes, used_events = [], set()
        for _, e in grp.iterrows():
            d = pd.Timestamp(e["start"])
            if d + post > today_ts or d < global_min:
                continue
            for s in series_list:
                before = s[(s.index <= d) & (s.index >= d - pd.Timedelta(days=5))]
                after = s[(s.index <= d + post) & (s.index >= d + post - pd.Timedelta(days=4))]
                if before.empty or after.empty or before.iloc[-1] <= 0:
                    continue
                changes.append((after.iloc[-1] / before.iloc[-1] - 1) * 100)
                used_events.add((e["name"], e["start"]))
        if changes:
            out[etype] = {"n_events": len(used_events), "n_obs": len(changes),
                          "median": float(np.median(changes))}
    return out


def upcoming_texts(events, effects, today):
    """Elenco (chiave, testo) degli eventi in arrivo o in corso."""
    if events.empty:
        return []
    df = events[events["type"].isin(["release", "release_other", "br_window", "rotation", "tournament"])].copy()
    df["s"] = pd.to_datetime(df["start"]).dt.date
    df["e"] = pd.to_datetime(df["end"]).dt.date
    horizon = today + timedelta(days=UPCOMING_DAYS)
    df = df[((df["s"] >= today) & (df["s"] <= horizon)) | ((df["s"] < today) & (df["e"] >= today))]
    out = []
    for _, e in df.sort_values("s").iterrows():
        approx = str(e["approx"]) == "True"
        if e["s"] < today:
            when = f"in corso fino al {e['e']:%d/%m}"
        elif e["s"] == today:
            when = "oggi"
        else:
            when = f"tra {(e['s'] - today).days} giorni ({e['s']:%d/%m})"
        text = f"- {e['name']}: {when}{' [stima]' if approx else ''}"
        eff = effects.get(e["type"])
        if eff and eff["n_events"] >= MIN_EVENTS_FOR_EFFECT and eff["n_obs"] >= MIN_OBS_FOR_EFFECT:
            text += (f"\n    storico: variazione mediana {eff['median']:+.1f}% nei {EFFECT_DAYS} giorni dopo "
                     f"eventi simili ({eff['n_events']} eventi, {eff['n_obs']} osservazioni)")
        else:
            text += "\n    storico ancora insufficiente per stimare l'effetto sui prezzi"
        out.append((f"{e['type']}:{e['name']}:{e['start']}", text))
    return out
