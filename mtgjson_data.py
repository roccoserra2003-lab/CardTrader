"""
Dati MTGJSON usati dal bot:
1) prezzi Cardmarket di OGGI (EUR), per controllare e integrare i prezzi di CardTrader
2) informazioni sulle carte (ristampe, Reserved List, EDHREC, Standard) per le etichette di rischio
   (il file data/card_info.csv viene creato dallo script di importazione)
"""
import gzip
import os
from datetime import date

import pandas as pd
import requests

import common as C

PRICES_TODAY_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.gz"
PROVIDER = "cardmarket"
CARD_INFO_FILE = "data/card_info.csv"
INFO_COLS = ["blueprint_id", "name", "set_code", "n_printings", "first_printing",
             "latest_printing", "is_reserved", "edhrec_rank", "standard"]

# Etichette di rischio
EDHREC_STAPLE_RANK = 1500     # "staple Commander" se il rank EDHREC e' entro 1500
RECENT_REPRINT_DAYS = 365     # "ristampata di recente" se l'ultima stampa e' entro 12 mesi
MANY_PRINTINGS = 4            # "ristampata spesso" da 4 stampe in su


# ------------------------------------------------------------ Prezzi di oggi
def open_stream(url):
    r = requests.get(url, stream=True, timeout=(30, 900), headers={"User-Agent": C.USER_AGENT})
    r.raise_for_status()
    return gzip.GzipFile(fileobj=r.raw)


def fetch_today_cardmarket(wanted, notes, opener=open_stream):
    """uuid -> {"normal": prezzo, "foil": prezzo, "etched": prezzo} (Cardmarket, vendita, EUR, oggi)."""
    out = {}
    try:
        import ijson
        stream = opener(PRICES_TODAY_URL)
        try:
            items = ijson.kvitems(stream, "data", use_float=True)
        except TypeError:                          # versioni vecchie di ijson
            items = ijson.kvitems(stream, "data")
        for uuid, data in items:
            if uuid not in wanted:
                continue
            cm = (((data or {}).get("paper") or {}).get(PROVIDER)) or {}
            if cm.get("currency", "EUR") != "EUR":
                continue
            retail = cm.get("retail") or {}
            rec = {}
            for finish in ("normal", "foil", "etched"):
                points = retail.get(finish) or {}
                if points:
                    rec[finish] = float(points[max(points)])     # data piu' recente
            if rec:
                out[uuid] = rec
    except Exception as e:
        notes.append(f"Controllo Cardmarket non disponibile oggi: {e}")
        return {}
    notes.append(f"Cardmarket: prezzi di oggi trovati per {len(out)} carte su {len(wanted)} richieste.")
    return out


# ------------------------------------------------------ Informazioni sulle carte
def load_card_info():
    """blueprint_id -> informazioni sulla carta (dict di testi). Vuoto se il file non esiste."""
    if not os.path.exists(CARD_INFO_FILE):
        return {}
    try:
        df = pd.read_csv(CARD_INFO_FILE, dtype=str).fillna("")
        return {int(r["blueprint_id"]): r for r in df.to_dict("records")}
    except Exception:
        return {}


def risk_labels(info, today):
    """Etichette di contesto per una carta (stampate nei messaggi di vendita)."""
    if not info:
        return []
    labels = []
    if str(info.get("is_reserved")).lower() == "true":
        labels.append("Reserved List (non ristampabile)")
    try:
        n = int(float(info.get("n_printings") or 0))
    except ValueError:
        n = 0
    latest = info.get("latest_printing") or ""
    if n >= 2 and latest:
        try:
            days = (today - date.fromisoformat(latest)).days
        except ValueError:
            days = None
        if days is not None and days < 0:
            labels.append(f"ristampa in arrivo il {latest}")
        elif days is not None and days <= RECENT_REPRINT_DAYS:
            labels.append(f"ristampata di recente (ultima stampa {latest}, {n} stampe in totale)")
        elif n >= MANY_PRINTINGS:
            labels.append(f"ristampata spesso ({n} stampe)")
    elif n >= MANY_PRINTINGS:
        labels.append(f"ristampata spesso ({n} stampe)")
    try:
        rank = int(float(info.get("edhrec_rank") or 0))
    except ValueError:
        rank = 0
    if 0 < rank <= EDHREC_STAPLE_RANK:
        labels.append(f"EDHREC #{rank}")
    if str(info.get("standard")).lower() == "true":
        labels.append("legale in Standard")
    return labels
