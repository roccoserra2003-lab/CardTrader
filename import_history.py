"""
Importazione una tantum dello storico prezzi (ultimi 90 giorni) da MTGJSON.
- Prende le carte che hai in vendita su CardTrader
- Le collega alle carte di MTGJSON (tramite l'ID Scryfall)
- Legge dal file AllPrices di MTGJSON i prezzi storici (Cardmarket, altrimenti TCGplayer ecc.)
- Salva tutto in data/history_mtgjson.csv: il bot lo usa per avere i trend da subito

Lo storico Cardmarket NON ha lo stesso livello di prezzo di CardTrader: il bot lo riscala
automaticamente sul primo giorno in cui ha un prezzo CardTrader (vedi build_series in bot.py).
"""
import gzip
import os
import sys
import time
import traceback

import pandas as pd
import requests

import common as C
import mtgjson_data as MJ

PRICES_URL = "https://mtgjson.com/api/v5/AllPrices.json.gz"
PROVIDERS = ["cardmarket", "tcgplayer", "cardkingdom", "cardsphere", "manapool"]   # in ordine di preferenza
OUT_FILE = "data/history_mtgjson.csv"


def load_mtgjson_set(code):
    """File MTGJSON di un singolo set (contiene le carte con uuid e ID Scryfall)."""
    name = code.upper()
    if name in {"CON", "PRN", "AUX", "NUL"}:      # nomi riservati su Windows: MTGJSON aggiunge "_"
        name += "_"
    data = C.web_get_json(f"https://mtgjson.com/api/v5/{name}.json", timeout=180, allow_404=True)
    return data.get("data") if data else None


def my_card_keys():
    """Insieme di (blueprint_id, foil) delle tue carte Magic in vendita."""
    keys = set()
    for p in C.ct_get("/products/export", timeout=180):
        if p.get("game_id") != C.MAGIC_GAME_ID:
            continue
        graded = p.get("graded")
        if graded and str(graded).lower() not in ("false", "0"):
            continue
        if (p.get("bundle_size") or 1) != 1:
            continue
        props = p.get("properties_hash") or {}
        foil = 1 if props.get("mtg_foil") in (True, "true", "True") else 0
        keys.add((p["blueprint_id"], foil))
    return keys


def blueprint_info(bp_ids, notes):
    """blueprint_id -> (scryfall_id, codice espansione) per le tue carte."""
    info = {}
    for exp in C.ct_get("/expansions/export"):
        if exp.get("game_id") != C.MAGIC_GAME_ID:
            continue
        try:
            blueprints = C.ct_get("/blueprints/export", {"expansion_id": exp["id"]}, timeout=120)
        except Exception as e:
            notes.append(f"Espansione {exp.get('name')}: blueprint non letti ({e})")
            continue
        time.sleep(0.5)
        for b in blueprints:
            if b["id"] in bp_ids and b.get("scryfall_id"):
                info[b["id"]] = (b["scryfall_id"], str(exp["code"]))
    return info


def scryfall_to_uuid(info, notes):
    """Collega gli ID Scryfall agli uuid di MTGJSON, leggendo i file dei singoli set."""
    by_set = {}
    for bp, (sf, code) in info.items():
        by_set.setdefault(code, []).append(sf)
    mapping, cards = {}, {}
    for code, sf_ids in by_set.items():
        data = None
        try:
            data = load_mtgjson_set(code)
            if data is None:                       # codice diverso: lo chiedo a Scryfall
                card = C.web_get_json(f"https://api.scryfall.com/cards/{sf_ids[0]}", allow_404=True)
                time.sleep(0.15)
                if card and card.get("set") and card["set"] != code:
                    data = load_mtgjson_set(card["set"])
        except Exception as e:
            notes.append(f"Set {code}: file MTGJSON non letto ({e})")
        if not data:
            notes.append(f"Set {code}: non trovato in MTGJSON ({len(sf_ids)} carte senza storico)")
            continue
        for c in data.get("cards", []):
            sf = (c.get("identifiers") or {}).get("scryfallId")
            if sf:
                mapping[sf] = c["uuid"]
                cards[c["uuid"]] = {
                    "name": c.get("name"), "printings": c.get("printings") or [],
                    "reserved": bool(c.get("isReserved")), "edhrec": c.get("edhrecRank"),
                    "standard": (c.get("legalities") or {}).get("standard") == "Legal",
                }
        time.sleep(0.3)
    return mapping, cards


def write_card_info(info, sf2uuid, cards, notes):
    """Salva data/card_info.csv: ristampe, Reserved List, EDHREC e legalita' Standard delle tue carte."""
    set_dates = {}
    try:
        set_dates = {s["code"].lower(): s.get("released_at") for s in C.scryfall_sets() if s.get("released_at")}
    except Exception as e:
        notes.append(f"Date dei set non lette da Scryfall ({e}): le ristampe non avranno la data")
    rows = []
    for bp, (sf, code) in info.items():
        card = cards.get(sf2uuid.get(sf))
        if not card:
            continue
        printings = [p.lower() for p in card["printings"]]
        dates = [set_dates[p] for p in printings if p in set_dates]
        rows.append({
            "blueprint_id": bp, "name": card["name"], "set_code": code, "n_printings": len(printings),
            "first_printing": min(dates) if dates else "", "latest_printing": max(dates) if dates else "",
            "is_reserved": card["reserved"], "edhrec_rank": card["edhrec"] or "", "standard": card["standard"],
        })
    if rows:                                   # non sovrascrivo il file esistente con uno vuoto
        pd.DataFrame(rows, columns=MJ.INFO_COLS).to_csv(MJ.CARD_INFO_FILE, index=False)
    return len(rows)


def open_prices_stream():
    """Apre in streaming il file AllPrices.json.gz (e' molto grande: non lo carichiamo tutto in memoria)."""
    r = requests.get(PRICES_URL, stream=True, timeout=(30, 900), headers={"User-Agent": C.USER_AGENT})
    r.raise_for_status()
    return gzip.GzipFile(fileobj=r.raw)


def read_prices(stream, wanted, stats):
    """Legge il file a pezzi e tiene solo i prezzi (di tutti i fornitori) delle carte volute."""
    import ijson
    try:
        items = ijson.kvitems(stream, "data", use_float=True)
    except TypeError:                              # versioni vecchie di ijson
        items = ijson.kvitems(stream, "data")
    found, seen = {}, 0
    for uuid, data in items:
        seen += 1
        if seen % 20000 == 0:
            print(f"  letti {seen} uuid, trovati {len(found)}/{len(wanted)}", flush=True)
        if uuid not in wanted:
            continue
        paper = (data or {}).get("paper") or {}
        entry = {}
        for prov in PROVIDERS:
            retail = (paper.get(prov) or {}).get("retail") or {}
            entry[prov] = {k: {d: float(p) for d, p in (retail.get(k) or {}).items()}
                           for k in ("normal", "foil", "etched")}
        found[uuid] = entry
        if len(found) == len(wanted):
            break                                  # trovate tutte: inutile leggere il resto
    stats["uuid_letti"] = seen
    stats["uuid_trovati"] = len(found)
    return found


def pick_prices(series, foil, provider_use):
    """Primo fornitore (in ordine di preferenza) che ha prezzi per quella versione della carta."""
    finishes = ("foil", "etched") if foil else ("normal",)
    for prov in PROVIDERS:
        for fin in finishes:
            prices = (series.get(prov) or {}).get(fin)
            if prices:
                provider_use[prov] = provider_use.get(prov, 0) + 1
                return prices
    return {}


def run():
    os.makedirs("data", exist_ok=True)
    notes, stats = [], {}
    keys = my_card_keys()
    bp_ids = {bp for bp, _ in keys}
    stats["carte_uniche"] = len(keys)
    print(f"Carte uniche in inventario: {len(keys)}", flush=True)

    info = blueprint_info(bp_ids, notes)
    stats["con_id_scryfall"] = f"{len(info)}/{len(bp_ids)}"
    print(f"Blueprint con ID Scryfall: {len(info)}/{len(bp_ids)}", flush=True)
    sf2uuid, cards = scryfall_to_uuid(info, notes)
    stats["id_scryfall_collegati"] = len(sf2uuid)

    uuid_keys = {}                                  # uuid -> [(blueprint_id, foil), ...]
    for bp, foil in keys:
        if bp in info and info[bp][0] in sf2uuid:
            uuid_keys.setdefault(sf2uuid[info[bp][0]], []).append((bp, foil))
    stats["uuid_voluti"] = len(uuid_keys)
    print(f"Carte collegate a MTGJSON: {len(uuid_keys)}", flush=True)
    stats["info_carte"] = write_card_info(info, sf2uuid, cards, notes)     # etichette di rischio
    print(f"Informazioni carte salvate: {stats['info_carte']}", flush=True)
    if not uuid_keys:
        raise RuntimeError("Nessuna delle tue carte e' stata collegata a MTGJSON. Dati: "
                           f"{stats}. Note: {' | '.join(notes[:6])}")

    found = read_prices(open_prices_stream(), set(uuid_keys), stats)
    print(f"Prezzi letti: {stats}", flush=True)

    rows, provider_use = [], {}
    for uuid, klist in uuid_keys.items():
        series = found.get(uuid)
        if not series:
            continue
        for bp, foil in klist:
            for d, price in pick_prices(series, foil, provider_use).items():
                if price > 0:
                    rows.append({"date": d, "blueprint_id": bp, "foil": foil, "cm_price": round(price, 2)})
    if not rows:
        raise RuntimeError(f"Nessun prezzo storico trovato per le tue carte. Dati: {stats}. "
                           f"Note: {' | '.join(notes[:6])}")

    df = pd.DataFrame(rows).sort_values(["blueprint_id", "foil", "date"])
    df.to_csv(OUT_FILE, index=False)

    n_cards = df.groupby(["blueprint_id", "foil"]).ngroups
    n_dates = df["date"].nunique()
    msg = [
        "MTG Bot - importazione storico completata",
        f"Carte con storico: {n_cards} su {len(keys)}",
        f"Date distinte: {n_dates} (dal {df['date'].min()} al {df['date'].max()})",
        f"Fonti usate (carte): {provider_use}. Il livello di prezzo viene riscalato su CardTrader.",
        f"Informazioni sulle carte (ristampe, EDHREC, Standard): {stats.get('info_carte', 0)} carte.",
    ]
    if n_dates < 20:
        msg.append("Attenzione: poche date distinte, lo storico potrebbe avere cadenza settimanale.")
    msg += [f"Nota: {n}" for n in notes[:12]]
    C.send_telegram("\n".join(msg))


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        traceback.print_exc()
        try:
            C.send_telegram(f"MTG Bot: errore nell'importazione dello storico.\n{e}")
        except Exception:
            pass
        sys.exit(1)
