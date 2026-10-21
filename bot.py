"""
MTG Price Bot - versione 2
- Segnali sulle carte che possiedi (da vendere / prezzo sotto mercato)
- EV dei box recenti (Play e Collector)
- Calendario eventi automatico (uscite, rotazione, finestra B&R, ban reali, tornei)
"""
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

import box_ev
import calendar_events as cal
import common as C

# =====================================================================
# IMPOSTAZIONI - puoi modificare questi numeri senza toccare il resto
# =====================================================================
MIN_PRICE = 2.00              # ignora i segnali di trend su carte sotto questo prezzo (EUR)
MIN_HISTORY_DAYS = 7          # giorni di storico minimi prima di dare segnali di trend

# Segnale "VENDI": serve un punteggio >= SELL_MIN_SCORE (ogni condizione vale 1 punto)
SELL_ABOVE_MED30_PCT = 25     # prezzo almeno +25% sopra la mediana a 30 giorni
SELL_SPIKE_7D_PCT = 20        # salita di almeno +20% in 7 giorni
SELL_ZSCORE = 2.0             # z-score (deviazioni standard sopra la media a 60 giorni)
SELL_NEAR_MAX90_PCT = 3       # entro il 3% dal massimo degli ultimi 90 giorni
SELL_MIN_SCORE = 2

# Segnale "PREZZO SOTTO MERCATO" (solo carte Near Mint)
UNDERPRICED_PCT = 15          # il mercato parte almeno 15% sopra il tuo prezzo
UNDERPRICED_MIN_EUR = 0.50    # e la differenza e' almeno 0,50 EUR

# True = ricevi ogni giorno un breve riepilogo anche quando non ci sono segnali
DAILY_SUMMARY = False

# Giorni prima di ripetere lo stesso avviso
COOLDOWN = {"sell": 7, "under": 7, "box": 3, "event": 30}
# =====================================================================

PRICES_FILE = "data/prices.csv"
CM_FILE = "data/history_mtgjson.csv"    # storico importato (opzionale): date, blueprint_id, foil, cm_price
ALERTS_FILE = "data/alerts_log.csv"
PRICE_COLS = ["date", "blueprint_id", "foil", "language", "name", "market", "min", "n_listings"]


# ----------------------------------------------------------- Inventario
def load_inventory():
    """Le tue carte Magic in vendita (singole, non gradate, 1 per bundle)."""
    products = C.ct_get("/products/export", timeout=180)
    items = []
    for p in products:
        if p.get("game_id") != C.MAGIC_GAME_ID:
            continue
        graded = p.get("graded")
        if graded and str(graded).lower() not in ("false", "0"):
            continue
        if (p.get("bundle_size") or 1) != 1:
            continue
        props = p.get("properties_hash") or {}
        items.append({
            "id": p["id"],
            "user_id": p.get("user_id"),
            "blueprint_id": p["blueprint_id"],
            "name": p.get("name_en", f"Blueprint {p['blueprint_id']}"),
            "quantity": p.get("quantity") or 1,
            "price": (p.get("price_cents") or 0) / 100,
            "foil": 1 if props.get("mtg_foil") in (True, "true", "True") else 0,
            "language": props.get("mtg_language") or "en",
            "condition": props.get("condition") or "Near Mint",
        })
    return items


def market_snapshot(blueprint_id, foil, language, my_user_id):
    data = C.ct_get("/marketplace/products", {
        "blueprint_id": blueprint_id,
        "foil": "true" if foil else "false",
        "language": language,
    })
    listings = data.get(str(blueprint_id), []) if isinstance(data, dict) else []
    return C.reference_price(listings, my_user_id)


# ------------------------------------------------------- Serie storiche
def build_series(ct_group, cm_group):
    """
    Serie dei prezzi di una carta. Se esiste lo storico importato (Cardmarket), lo si "aggancia"
    ai prezzi CardTrader riscalandolo sul primo giorno in cui abbiamo il dato CardTrader,
    cosi' i due livelli di prezzo non creano salti fittizi.
    """
    ct = pd.Series(ct_group["market"].values, index=pd.to_datetime(ct_group["date"])).sort_index()
    if cm_group is None or cm_group.empty or ct.empty:
        return ct
    cm = pd.Series(cm_group["cm_price"].values, index=pd.to_datetime(cm_group["date"])).sort_index()
    first_ct = ct.index[0]
    before = cm[cm.index <= first_ct]
    if before.empty or before.iloc[-1] <= 0:
        return ct
    ratio = ct.iloc[0] / before.iloc[-1]
    scaled = cm[cm.index < first_ct] * ratio
    return pd.concat([scaled, ct]) if not scaled.empty else ct


def analyse(series, today_ts):
    s = series.sort_index()
    price = float(s.iloc[-1])
    prev = s.iloc[:-1]
    if len(prev) < MIN_HISTORY_DAYS:
        return None
    day = pd.Timedelta(days=1)
    last30 = prev[prev.index >= today_ts - 30 * day]
    last60 = prev[prev.index >= today_ts - 60 * day]
    last90 = s[s.index >= today_ts - 90 * day]
    med30 = float(last30.median()) if len(last30) >= 3 else None
    zscore = None
    if len(last60) >= MIN_HISTORY_DAYS:
        mean = float(last60.mean())
        std = max(float(last60.std()), 0.02 * mean)   # soglia minima: evita z-score assurdi su prezzi quasi fermi
        if std > 0:
            zscore = (price - mean) / std
    chg7 = None
    old = prev[prev.index <= today_ts - 7 * day]
    if len(old) and old.index[-1] >= today_ts - 14 * day:
        chg7 = (price / float(old.iloc[-1]) - 1) * 100
    return {"price": price, "med30": med30, "z": zscore, "chg7": chg7, "max90": float(last90.max())}


def sell_signal(st):
    score, reasons = 0, []
    price = st["price"]
    if st["med30"]:
        diff = (price / st["med30"] - 1) * 100
        if diff >= SELL_ABOVE_MED30_PCT:
            score += 1
            reasons.append(f"+{diff:.0f}% sopra la mediana 30 gg ({st['med30']:.2f} EUR)")
    if st["chg7"] is not None and st["chg7"] >= SELL_SPIKE_7D_PCT:
        score += 1
        reasons.append(f"+{st['chg7']:.0f}% in 7 giorni")
    if st["z"] is not None and st["z"] >= SELL_ZSCORE:
        score += 1
        reasons.append(f"z-score {st['z']:.1f} (molto sopra la norma)")
    if price >= st["max90"] * (1 - SELL_NEAR_MAX90_PCT / 100):
        score += 1
        reasons.append("vicino al massimo a 90 giorni")
    return score, reasons


def label(name, foil, lang):
    return f"{name} ({'foil, ' if foil else ''}{lang.upper()})"


def inventory_part(today):
    today_iso, today_ts = today.isoformat(), pd.Timestamp(today)
    inventory = load_inventory()
    stats = {"monitored": 0, "priced": 0, "days": 0, "names": set()}
    if not inventory:
        return [], {}, stats
    my_user_id = inventory[0]["user_id"]

    groups = {}
    for item in inventory:
        groups.setdefault((item["blueprint_id"], item["foil"], item["language"]), []).append(item)

    # Prezzi di mercato di oggi (1 chiamata al secondo per rispettare i limiti API)
    snapshots, new_rows = {}, []
    for key, items in groups.items():
        bp, foil, lang = key
        snap = market_snapshot(bp, foil, lang, my_user_id)
        time.sleep(1.1)
        if not snap:
            continue
        snapshots[key] = snap
        new_rows.append({
            "date": today_iso, "blueprint_id": bp, "foil": foil, "language": lang,
            "name": items[0]["name"], "market": round(snap["market"], 2),
            "min": round(snap["min"], 2), "n_listings": snap["n"],
        })

    # Salva lo storico (se rilanci nello stesso giorno, sostituisce i dati di oggi)
    hist = C.load_csv(PRICES_FILE, PRICE_COLS)
    hist = hist[hist["date"] != today_iso]
    new_df = pd.DataFrame(new_rows, columns=PRICE_COLS)
    frames = [f for f in (hist, new_df) if not f.empty]
    hist = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PRICE_COLS)
    for col in ("blueprint_id", "foil"):
        hist[col] = hist[col].astype(int)
    for col in ("market", "min", "n_listings"):
        hist[col] = pd.to_numeric(hist[col])
    hist.to_csv(PRICES_FILE, index=False)

    cm_by_key = {}
    if os.path.exists(CM_FILE):
        cm = pd.read_csv(CM_FILE)
        cm_by_key = {k: g for k, g in cm.groupby(["blueprint_id", "foil"])}

    series_by_key = {}
    if not hist.empty:
        for key, g in hist.groupby(["blueprint_id", "foil", "language"]):
            series_by_key[key] = build_series(g, cm_by_key.get((key[0], key[1])))

    alerts = []
    for key, items in groups.items():
        snap = snapshots.get(key)
        if not snap or snap["n"] < 2:
            continue                      # troppo poche offerte: prezzo poco affidabile
        bp, foil, lang = key
        name = label(items[0]["name"], foil, lang)
        qty = sum(i["quantity"] for i in items)
        market = snap["market"]

        # a) prezzo sotto mercato (funziona dal primo giorno)
        for item in items:
            if item["condition"] != "Near Mint" or item["price"] <= 0:
                continue
            gap = market - item["price"]
            if gap >= UNDERPRICED_MIN_EUR and gap / item["price"] * 100 >= UNDERPRICED_PCT:
                alerts.append(("under", str(item["id"]),
                               f"- {name} x{item['quantity']}: il tuo prezzo e' {item['price']:.2f} EUR, "
                               f"il mercato parte da {market:.2f} EUR (+{gap / item['price'] * 100:.0f}%)"))

        # b) segnale "vendi ora" (serve storico)
        if market < MIN_PRICE or key not in series_by_key:
            continue
        st = analyse(series_by_key[key], today_ts)
        if not st:
            continue
        score, reasons = sell_signal(st)
        if score >= SELL_MIN_SCORE:
            alerts.append(("sell", str(key),
                           f"- {name} x{qty}: ora {st['price']:.2f} EUR\n    " + "\n    ".join(reasons)))

    stats.update({"monitored": len(groups), "priced": len(snapshots),
                  "days": hist["date"].nunique() if not hist.empty else 0,
                  "names": {i["name"].lower() for i in inventory}})
    return alerts, series_by_key, stats


# ------------------------------------------------------------- Messaggi
def section(title, lines):
    return f"{title}\n" + "\n".join(lines) + "\n" if lines else ""


def ban_lines(ban_changes, owned_names):
    lines = []
    for c in ban_changes:
        for kind, names in (("BANDITE", c["banned"]), ("LIBERATE", c["unbanned"])):
            if not names:
                continue
            marked = [f"{n} (la possiedi!)" if n.lower() in owned_names else n for n in names]
            lines.append(f"- {c['format'].capitalize()} - {kind}: " + ", ".join(marked))
    return lines


def run():
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    os.makedirs("data", exist_ok=True)
    errors, notes, alerts = [], [], []

    # 1) Ban reali e calendario eventi
    ban_changes, events = [], pd.DataFrame(columns=cal.EVENT_COLS)
    try:
        ban_changes = cal.detect_ban_changes(notes)
    except Exception as e:
        errors.append(f"Rilevamento ban: {e}")
    try:
        events = cal.collect_events(today, notes, ban_changes)
    except Exception as e:
        errors.append(f"Calendario eventi: {e}")

    # 2) Inventario e segnali di vendita
    series_by_key, stats = {}, {"monitored": 0, "priced": 0, "days": 0, "names": set()}
    try:
        inv_alerts, series_by_key, stats = inventory_part(today)
        alerts += inv_alerts
    except Exception as e:
        traceback.print_exc()
        errors.append(f"Inventario: {e}")

    # 3) Eventi in arrivo con effetto storico
    try:
        effects = cal.event_effects(events, series_by_key, today)
        for key, text in cal.upcoming_texts(events, effects, today):
            alerts.append(("event", key, text))
    except Exception as e:
        errors.append(f"Analisi eventi: {e}")

    # 4) EV dei box
    box_results = []
    try:
        box_results = box_ev.evaluate_all(today, notes)
        box_ev.save_results(box_results, today)
        for r in box_results:
            if box_ev.is_alert(r):
                alerts.append(("box", f"{r['code']}:{r['product']}", box_ev.format_line(r)))
    except Exception as e:
        traceback.print_exc()
        errors.append(f"EV box: {e}")

    # 5) Evita di ripetere lo stesso avviso ogni giorno
    log = C.load_csv(ALERTS_FILE, ["date", "type", "key"])
    recent = {t: C.recent_alerts(log, today, d) for t, d in COOLDOWN.items()}
    fresh = [a for a in alerts if (a[0], a[1]) not in recent.get(a[0], set())]

    # 6) Messaggio
    by_type = lambda t: [a[2] for a in fresh if a[0] == t]
    parts = [
        section("EV BOX SOPRA IL PREZZO (rapporto EV netto / prezzo box)", by_type("box")),
        section("VENDI ORA (prezzo alto rispetto allo storico)", by_type("sell")),
        section("PREZZO SOTTO IL MERCATO (potresti alzarlo)", by_type("under")),
        section("BAN / UNBAN", ban_lines(ban_changes, stats["names"])),
        section("EVENTI IN ARRIVO", by_type("event")),
        section("AVVISI", [n for n in notes if n.startswith("AVVISO:")]),
        section("ERRORI", [f"- {e}" for e in errors]),
    ]
    body = "\n".join(p for p in parts if p)

    if manual or DAILY_SUMMARY:
        top = sorted(box_results, key=lambda r: r["ratio"], reverse=True)[:3]
        status = [
            f"Carte monitorate: {stats['monitored']} (prezzi trovati per {stats['priced']}), "
            f"giorni di storico: {stats['days']}",
            f"Eventi nel calendario: {len(events)}",
            f"Box valutati: {len(box_results)}",
        ]
        status += [f"  {r['set']} - {r['product']}: x{r['ratio']:.2f} ({r['packs']} bustine"
                   f"{'' if r['packs_source'] == 'mtgjson' else ', NON verificate'})" for r in top]
        status += [f"Nota: {n}" for n in notes if not n.startswith("AVVISO:")][:8]
        body += "\n" + section("STATO", status)

    if body.strip():
        C.send_telegram(f"MTG Bot - {today_iso}\n\n{body}")

    if fresh:
        new_log = pd.DataFrame([{"date": today_iso, "type": a[0], "key": a[1]} for a in fresh])
        frames = [f for f in (log, new_log) if not f.empty]
        pd.concat(frames, ignore_index=True).to_csv(ALERTS_FILE, index=False)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        try:
            C.send_telegram(f"MTG Bot: errore durante l'esecuzione.\n{e}")
        except Exception:
            pass
        sys.exit(1)
