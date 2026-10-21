"""
MTG Price Bot - Fase 1
- Legge il tuo inventario su CardTrader
- Salva ogni giorno il prezzo di mercato di ogni tua carta (storico)
- Ti manda su Telegram:
    1) carte da VENDERE ORA (prezzo alto rispetto allo storico)
    2) carte vicine al MINIMO (non vendere / eventuale acquisto)
    3) carte messe in vendita a un prezzo SOTTO il mercato
"""
import os
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone

import pandas as pd
import requests

# =====================================================================
# IMPOSTAZIONI - puoi modificare questi numeri senza toccare il resto
# =====================================================================
MIN_PRICE = 2.00              # ignora i segnali su carte sotto questo prezzo (EUR)
MIN_HISTORY_DAYS = 7          # giorni di storico minimi prima di dare segnali di trend

# Segnale "VENDI": serve un punteggio >= SELL_MIN_SCORE (ogni condizione vale 1 punto)
SELL_ABOVE_MED30_PCT = 25     # prezzo almeno +25% sopra la mediana a 30 giorni
SELL_SPIKE_7D_PCT = 20        # salita di almeno +20% in 7 giorni
SELL_ZSCORE = 2.0             # z-score (deviazioni standard sopra la media a 60 gg)
SELL_NEAR_MAX90_PCT = 3       # entro il 3% dal massimo degli ultimi 90 giorni
SELL_MIN_SCORE = 2

# Segnale "MINIMO"
LOW_NEAR_MIN_PCT = 5          # entro il 5% dal minimo a 90 giorni...
LOW_ZSCORE = -1.5             # ...e z-score <= -1.5 (oppure 15% sotto la mediana 30 gg)

# Segnale "PREZZO SOTTO MERCATO" (solo carte Near Mint)
UNDERPRICED_PCT = 15          # il mercato parte almeno 15% sopra il tuo prezzo
UNDERPRICED_MIN_EUR = 0.50    # e la differenza e' almeno 0,50 EUR

COOLDOWN_DAYS = 7             # non ripete lo stesso avviso per la stessa carta prima di 7 giorni
# =====================================================================

API = "https://api.cardtrader.com/api/v2"
MAGIC_GAME_ID = 1
PRICES_FILE = "data/prices.csv"
ALERTS_FILE = "data/alerts_log.csv"
PRICE_COLS = ["date", "blueprint_id", "foil", "language", "name", "market", "min", "n_listings"]

CT_TOKEN = os.environ["CARDTRADER_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
HEADERS = {"Authorization": f"Bearer {CT_TOKEN}"}


# ---------------------------------------------------------------- Telegram
def send_telegram(text):
    """Invia un messaggio, spezzandolo se troppo lungo."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )


# --------------------------------------------------------------- CardTrader
def api_get(path, params=None, timeout=60, retries=4):
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}{path}", headers=HEADERS, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Chiamata a {path} fallita: {last_error}")


def load_inventory():
    """Le tue carte Magic in vendita (singole, non gradate, 1 per bundle)."""
    products = api_get("/products/export", timeout=180)
    items = []
    for p in products:
        if p.get("game_id") != MAGIC_GAME_ID:
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
    """
    Prezzo di mercato attuale = mediana delle 3 offerte piu' economiche
    (Near Mint/Mint, escludendo te stesso e chi e' in vacanza).
    L'API restituisce al massimo le 25 offerte piu' economiche.
    """
    data = api_get("/marketplace/products", {
        "blueprint_id": blueprint_id,
        "foil": "true" if foil else "false",
        "language": language,
    })
    listings = data.get(str(blueprint_id), []) if isinstance(data, dict) else []
    prices = []
    for lst in listings:
        if lst.get("on_vacation") or lst.get("graded"):
            continue
        if (lst.get("bundle_size") or 1) != 1:
            continue
        if (lst.get("user") or {}).get("id") == my_user_id:
            continue
        condition = (lst.get("properties_hash") or {}).get("condition")
        if condition not in ("Near Mint", "Mint"):
            continue
        prices.append(lst["price"]["cents"] / 100)
    if not prices:
        return None
    prices.sort()
    return {"market": statistics.median(prices[:3]), "min": prices[0], "n": len(prices)}


# ------------------------------------------------------------------ Storico
def load_csv(path, columns):
    if os.path.exists(path):
        return pd.read_csv(path, dtype={"language": str, "date": str, "type": str, "key": str})
    return pd.DataFrame(columns=columns)


def analyse(series, today_ts):
    """Calcola le statistiche di trend per una carta. series = prezzi indicizzati per data."""
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
        std = float(last60.std())
        if std > 0:
            zscore = (price - float(last60.mean())) / std

    chg7 = None
    old = prev[prev.index <= today_ts - 7 * day]
    if len(old) and old.index[-1] >= today_ts - 14 * day:
        chg7 = (price / float(old.iloc[-1]) - 1) * 100

    return {
        "price": price,
        "med30": med30,
        "z": zscore,
        "chg7": chg7,
        "max90": float(last90.max()),
        "min90": float(last90.min()),
    }


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


def low_signal(st):
    price = st["price"]
    near_min = price <= st["min90"] * (1 + LOW_NEAR_MIN_PCT / 100)
    weak = (st["z"] is not None and st["z"] <= LOW_ZSCORE) or (
        st["med30"] is not None and price <= st["med30"] * 0.85
    )
    if near_min and weak:
        reasons = [f"minimo a 90 gg: {st['min90']:.2f} EUR"]
        if st["med30"]:
            reasons.append(f"mediana 30 gg: {st['med30']:.2f} EUR")
        return reasons
    return None


def label(name, foil, lang):
    return f"{name} ({'foil, ' if foil else ''}{lang.upper()})"


# --------------------------------------------------------------------- Main
def run():
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    today_ts = pd.Timestamp(today)
    manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    os.makedirs("data", exist_ok=True)

    # 1) Inventario
    inventory = load_inventory()
    if not inventory:
        send_telegram("Nessuna carta Magic trovata nel tuo inventario CardTrader.")
        return
    my_user_id = inventory[0]["user_id"]

    groups = {}
    for item in inventory:
        key = (item["blueprint_id"], item["foil"], item["language"])
        groups.setdefault(key, []).append(item)

    # 2) Prezzi di mercato di oggi (1 chiamata al secondo per rispettare i limiti API)
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

    # 3) Salva lo storico (se rilanci nello stesso giorno, sostituisce i dati di oggi)
    hist = load_csv(PRICES_FILE, PRICE_COLS)
    hist = hist[hist["date"] != today_iso]
    hist = pd.concat([hist, pd.DataFrame(new_rows, columns=PRICE_COLS)], ignore_index=True)
    hist["blueprint_id"] = hist["blueprint_id"].astype(int)
    hist["foil"] = hist["foil"].astype(int)
    hist.to_csv(PRICES_FILE, index=False)
    days_of_history = hist["date"].nunique()

    by_key = {k: g for k, g in hist.groupby(["blueprint_id", "foil", "language"])}

    # 4) Cerca i segnali
    alerts = []  # (tipo, chiave_dedup, testo)
    for key, items in groups.items():
        snap = snapshots.get(key)
        if not snap or snap["n"] < 2:
            continue  # troppo poche offerte: prezzo poco affidabile
        bp, foil, lang = key
        name = label(items[0]["name"], foil, lang)
        qty = sum(i["quantity"] for i in items)
        market = snap["market"]

        # a) prezzo sotto mercato (funziona da subito, senza storico)
        for item in items:
            if item["condition"] != "Near Mint":
                continue
            gap = market - item["price"]
            if item["price"] > 0 and gap >= UNDERPRICED_MIN_EUR and gap / item["price"] * 100 >= UNDERPRICED_PCT:
                alerts.append((
                    "under", f"{item['id']}",
                    f"- {name} x{item['quantity']}: il tuo prezzo e' {item['price']:.2f} EUR, "
                    f"il mercato parte da {market:.2f} EUR (+{gap / item['price'] * 100:.0f}%)",
                ))

        # b) segnali di trend (servono almeno MIN_HISTORY_DAYS giorni di storico)
        if market < MIN_PRICE or key not in by_key:
            continue
        g = by_key[key]
        series = pd.Series(g["market"].values, index=pd.to_datetime(g["date"]))
        st = analyse(series, today_ts)
        if not st:
            continue

        score, reasons = sell_signal(st)
        if score >= SELL_MIN_SCORE:
            alerts.append((
                "sell", str(key),
                f"- {name} x{qty}: ora {st['price']:.2f} EUR\n    " + "\n    ".join(reasons),
            ))
        low = low_signal(st)
        if low:
            alerts.append((
                "low", str(key),
                f"- {name} x{qty}: ora {st['price']:.2f} EUR\n    " + "\n    ".join(low),
            ))

    # 5) Evita di ripetere lo stesso avviso ogni giorno
    log = load_csv(ALERTS_FILE, ["date", "type", "key"])
    cutoff = (today_ts - pd.Timedelta(days=COOLDOWN_DAYS)).date().isoformat()
    recent = set(zip(log[log["date"] >= cutoff]["type"], log[log["date"] >= cutoff]["key"]))
    fresh = [a for a in alerts if (a[0], a[1]) not in recent]

    # 6) Messaggio
    sections = [
        ("sell", "VENDI ORA (prezzo alto rispetto allo storico)"),
        ("low", "VICINE AL MINIMO (meglio non vendere ora)"),
        ("under", "PREZZO SOTTO IL MERCATO (potresti alzarlo)"),
    ]
    lines = []
    for atype, title in sections:
        rows = [a[2] for a in fresh if a[0] == atype]
        if rows:
            lines.append(f"{title}\n" + "\n".join(rows) + "\n")

    if lines:
        send_telegram("MTG Bot - " + today_iso + "\n\n" + "\n".join(lines))
        new_log = pd.DataFrame([{"date": today_iso, "type": a[0], "key": a[1]} for a in fresh])
        log = pd.concat([log, new_log], ignore_index=True)
        log.to_csv(ALERTS_FILE, index=False)
    elif manual_run:
        send_telegram(
            f"MTG Bot funzionante.\nCarte monitorate: {len(groups)} (prezzi trovati per {len(snapshots)}).\n"
            f"Giorni di storico: {days_of_history} (i segnali di trend partono dopo {MIN_HISTORY_DAYS}).\n"
            "Nessun segnale al momento."
        )


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        traceback.print_exc()
        try:
            send_telegram(f"MTG Bot: errore durante l'esecuzione.\n{e}")
        except Exception:
            pass
        sys.exit(1)
