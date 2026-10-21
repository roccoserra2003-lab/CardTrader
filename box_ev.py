"""
Valore atteso (EV) dei box sigillati.
- Struttura delle bustine (fogli di carte e pesi): MTGJSON
- Prezzi delle carte e del box: CardTrader
- EV esatto + simulazione di migliaia di box (distribuzione dei risultati)
"""
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

import common as C

# =====================================================================
# IMPOSTAZIONI
# =====================================================================
PRODUCTS = [
    # booster_key = nome della configurazione in MTGJSON
    # packs = bustine per box USATE SOLO SE MTGJSON non le indica (cambiano da set a set: es. 30 o 36)
    {"label": "Play Booster Box", "booster_key": "play", "packs": 36, "name_words": ["play", "booster"]},
    {"label": "Collector Booster Box", "booster_key": "collector", "packs": 12, "name_words": ["collector", "booster"]},
]
SET_TYPES = {"expansion", "core", "masters", "draft_innovation"}
MONTHS_BACK = 12              # set usciti negli ultimi 12 mesi
MIN_CARD_VALUE = 0.30         # sotto questo prezzo (EUR) una carta vale 0: nella pratica non si vende
SELL_FEE_PCT = 5.0            # commissione di vendita stimata (da verificare sul tuo account CardTrader)
EV_ALERT_RATIO = 1.10         # avvisa se EV netto / prezzo box >= 1,10
MIN_COVERAGE = 0.90           # quota minima di carte con prezzo noto, altrimenti non avvisa
SIMULATIONS = 5000            # box simulati per ogni prodotto
# =====================================================================

EXCLUDE_WORDS = ("case", "bundle", "sample", "prerelease", "japan")
RESULTS_FILE = "data/box_ev.csv"
RESULT_COLS = ["date", "set_code", "set_name", "product", "box_price", "ev_net", "ratio",
               "prob_profit", "median", "p10", "p90", "coverage"]


# ------------------------------------------------------------ Dati di base
def recent_sets(today):
    out = []
    for s in C.scryfall_sets():
        if s.get("digital") or s.get("parent_set_code") or not s.get("released_at"):
            continue
        if s.get("set_type") not in SET_TYPES:
            continue
        released = date.fromisoformat(s["released_at"])
        if today - timedelta(days=MONTHS_BACK * 30) <= released <= today:
            out.append({"code": s["code"], "name": s["name"], "released": released})
    return sorted(out, key=lambda x: x["released"], reverse=True)


def ct_expansion_index():
    by_code, by_name, exps = {}, {}, []
    for e in C.ct_get("/expansions"):
        if e.get("game_id") != C.MAGIC_GAME_ID:
            continue
        by_code[str(e["code"]).lower()] = e["id"]
        by_name[str(e["name"]).lower()] = e["id"]
        exps.append({"id": e["id"], "name": str(e["name"])})
    return by_code, by_name, exps


def related_expansions(set_name, exp_id, exps):
    """Altre espansioni CardTrader con il nome del set dentro (es. una sezione 'Sealed')."""
    key = set_name.lower()
    return [e for e in exps if key in e["name"].lower() and e["id"] != exp_id][:3]


def box_like_names(blueprints, limit=10):
    """Nomi di prodotti sigillati trovati, utili per capire come li chiama CardTrader."""
    words = ("booster", "box", "display", "bundle", "case")
    names = [str(b.get("name", "")) for b in blueprints if any(w in str(b.get("name", "")).lower() for w in words)]
    return names[:limit]


def packs_per_box(set_data, words, default):
    """Numero di bustine per box letto da MTGJSON (cambia da set a set). Ritorna (numero, fonte)."""
    for sp in set_data.get("sealedProduct") or []:
        n = str(sp.get("name", "")).lower()
        if not all(w in n for w in words) or not ("box" in n or "display" in n):
            continue
        if any(x in n for x in EXCLUDE_WORDS):
            continue
        sealed = (sp.get("contents") or {}).get("sealed") or []
        count = sum(int(x.get("count", 0)) for x in sealed
                    if "booster" in str(x.get("name", "")).lower() or "pack" in str(x.get("name", "")).lower())
        if count > 0:
            return count, "mtgjson"
    return default, "predefinito"


def load_mtgjson_set(code):
    name = code.upper()
    if name in {"CON", "PRN", "AUX", "NUL"}:      # nomi riservati su Windows: MTGJSON aggiunge "_"
        name += "_"
    data = C.web_get_json(f"https://mtgjson.com/api/v5/{name}.json", timeout=180, allow_404=True)
    return data.get("data") if data else None


def expansion_prices(expansion_id, foil):
    """Prezzo di riferimento di ogni carta del set (inglese, Near Mint), una sola chiamata."""
    data = C.ct_get("/marketplace/products", {
        "expansion_id": expansion_id, "foil": "true" if foil else "false", "language": "en"}, timeout=240)
    time.sleep(1.1)
    prices = {}
    if isinstance(data, dict):
        for bp, listings in data.items():
            ref = C.reference_price(listings)
            if ref:
                prices[int(bp)] = ref["market"]
    return prices


def find_box_blueprint(blueprints, words):
    best = None
    for b in blueprints:
        n = str(b.get("name", "")).lower()
        if not all(w in n for w in words):
            continue
        if not ("box" in n or "display" in n):
            continue
        if any(x in n for x in EXCLUDE_WORDS):
            continue
        if best is None or len(n) < len(str(best.get("name", ""))):
            best = b
    return best


def box_price(blueprint_id):
    for params in ({"blueprint_id": blueprint_id, "language": "en"}, {"blueprint_id": blueprint_id}):
        data = C.ct_get("/marketplace/products", params)
        time.sleep(1.1)
        listings = data.get(str(blueprint_id), []) if isinstance(data, dict) else []
        ref = C.reference_price(listings, check_condition=False)
        if ref:
            return ref
    return None


# ----------------------------------------------------------- Modello e EV
def build_model(booster_cfg, uuid2sf, sf2bp, prices_nf, prices_f):
    """Trasforma la configurazione MTGJSON + i prezzi in un modello calcolabile."""
    keep = 1 - SELL_FEE_PCT / 100
    sheets = {}
    for name, sh in (booster_cfg.get("sheets") or {}).items():
        cards = sh.get("cards") or {}
        is_foil = bool(sh.get("foil"))
        weights, values, covered = [], [], []
        for uuid, w in cards.items():
            bp = sf2bp.get(uuid2sf.get(uuid))
            price = None
            if bp is not None:
                price = prices_f.get(bp, prices_nf.get(bp)) if is_foil else prices_nf.get(bp)
            covered.append(price is not None)
            p = price or 0.0
            values.append(0.0 if p < MIN_CARD_VALUE else p * keep)
            weights.append(float(w))
        if not weights or sum(weights) <= 0:
            continue
        w_arr, v_arr = np.array(weights), np.array(values)
        sheets[name] = {
            "values": v_arr,
            "cdf": np.cumsum(w_arr),
            "mean": float((w_arr * v_arr).sum() / w_arr.sum()),
            "cov": float((w_arr * np.array(covered)).sum() / w_arr.sum()),
            "fixed": bool(sh.get("fixed")),
            "fixed_sum": float(v_arr.sum()),
        }
    boosters = booster_cfg.get("boosters") or []
    if isinstance(boosters, dict):
        boosters = [b for lst in boosters.values() for b in lst]
    configs = [{"contents": b.get("contents") or {}, "weight": float(b.get("weight", 1))} for b in boosters]
    configs = [c for c in configs if c["weight"] > 0]
    return {"sheets": sheets, "configs": configs}


def pack_ev_and_coverage(model):
    configs, sheets = model["configs"], model["sheets"]
    total_w = sum(c["weight"] for c in configs)
    if total_w <= 0:
        return 0.0, 0.0
    ev, cov_num, cov_den = 0.0, 0.0, 0.0
    for c in configs:
        p = c["weight"] / total_w
        for sname, count in c["contents"].items():
            sh = sheets.get(sname)
            if not sh:
                continue
            if sh["fixed"]:
                ev += p * sh["fixed_sum"]
            else:
                ev += p * count * sh["mean"]
                cov_num += p * count * sh["cov"]
                cov_den += p * count
    return ev, (cov_num / cov_den if cov_den else 0.0)


def simulate_boxes(model, packs, n_sims, rng):
    """Simula n_sims box. Ignora il divieto di duplicati nella stessa bustina (effetto minimo sull'EV)."""
    configs, sheets = model["configs"], model["sheets"]
    probs = np.array([c["weight"] for c in configs])
    probs = probs / probs.sum()
    counts = rng.multinomial(packs, probs, size=n_sims)          # bustine per configurazione, per box
    totals = np.zeros(n_sims)
    for sname, sh in sheets.items():
        per_cfg = np.array([c["contents"].get(sname, 0) for c in configs], dtype=float)
        if sh["fixed"]:
            totals += (counts @ (per_cfg > 0).astype(float)) * sh["fixed_sum"]
            continue
        draws = (counts @ per_cfg).astype(int)                    # estrazioni da questo foglio, per box
        n_total = int(draws.sum())
        if n_total == 0 or sh["cdf"][-1] <= 0:
            continue
        idx = np.searchsorted(sh["cdf"], rng.random(n_total) * sh["cdf"][-1], side="right")
        idx = np.minimum(idx, len(sh["values"]) - 1)
        box_ids = np.repeat(np.arange(n_sims), draws)
        totals += np.bincount(box_ids, weights=sh["values"][idx], minlength=n_sims)
    return totals


def evaluate_product(model, packs, price, rng):
    pack_ev, coverage = pack_ev_and_coverage(model)
    ev_box = pack_ev * packs
    totals = simulate_boxes(model, packs, SIMULATIONS, rng)
    return {
        "ev_net": ev_box,
        "ratio": ev_box / price if price > 0 else 0.0,
        "prob_profit": float((totals >= price).mean()),
        "median": float(np.median(totals)),
        "p10": float(np.percentile(totals, 10)),
        "p90": float(np.percentile(totals, 90)),
        "coverage": coverage,
    }


# --------------------------------------------------------------- Principale
def evaluate_all(today, notes):
    results = []
    rng = np.random.default_rng(int(today.strftime("%Y%m%d")))
    sets = recent_sets(today)
    by_code, by_name, all_exps = ct_expansion_index()
    for s in sets:
        try:
            set_data = load_mtgjson_set(s["code"])
            booster = (set_data or {}).get("booster") or {}
            products = [p for p in PRODUCTS if p["booster_key"] in booster]
            if not products:
                keys = ", ".join(sorted(booster.keys())) or "nessuna"
                notes.append(f"{s['name']}: nessuna bustina Play/Collector in MTGJSON (disponibili: {keys}).")
                continue
            exp_id = by_code.get(s["code"].lower()) or by_name.get(s["name"].lower())
            if not exp_id:
                notes.append(f"{s['name']}: set non trovato su CardTrader.")
                continue
            blueprints = C.ct_get("/blueprints/export", {"expansion_id": exp_id}, timeout=120)
            time.sleep(0.5)
            sf2bp = {b["scryfall_id"]: b["id"] for b in blueprints if b.get("scryfall_id")}
            uuid2sf = {c["uuid"]: (c.get("identifiers") or {}).get("scryfallId")
                       for c in set_data.get("cards", [])}
            prices_nf = expansion_prices(exp_id, foil=False)
            prices_f = expansion_prices(exp_id, foil=True)
            related_bps = None
            for p in products:
                box_bp = find_box_blueprint(blueprints, p["name_words"])
                if not box_bp:
                    # ripiego: i prodotti sigillati potrebbero stare in un'altra sezione del set
                    if related_bps is None:
                        related_bps = []
                        for rel in related_expansions(s["name"], exp_id, all_exps):
                            try:
                                related_bps += C.ct_get("/blueprints/export", {"expansion_id": rel["id"]}, timeout=120)
                            except Exception:
                                pass
                            time.sleep(0.5)
                    box_bp = find_box_blueprint(related_bps, p["name_words"])
                if not box_bp:
                    seen = box_like_names(blueprints + related_bps) or ["nessuno"]
                    notes.append(f"{s['name']}: {p['label']} non trovato su CardTrader. "
                                 f"Prodotti simili: {'; '.join(seen)}")
                    continue
                ref = box_price(box_bp["id"])
                if not ref:
                    notes.append(f"{s['name']}: nessuna offerta per {p['label']}.")
                    continue
                model = build_model(booster[p["booster_key"]], uuid2sf, sf2bp, prices_nf, prices_f)
                if not model["sheets"] or not model["configs"]:
                    notes.append(f"{s['name']}: struttura bustina non utilizzabile per {p['label']}.")
                    continue
                packs, packs_source = packs_per_box(set_data, p["name_words"], p["packs"])
                metrics = evaluate_product(model, packs, ref["market"], rng)
                results.append({"code": s["code"], "set": s["name"], "product": p["label"],
                                "box_price": ref["market"], "packs": packs,
                                "packs_source": packs_source, **metrics})
        except Exception as e:
            notes.append(f"{s['name']}: errore nel calcolo EV ({e}).")
    return results


def save_results(results, today):
    iso = today.isoformat()
    old = C.load_csv(RESULTS_FILE, RESULT_COLS)
    old = old[old["date"] != iso]
    rows = [{"date": iso, "set_code": r["code"], "set_name": r["set"], "product": r["product"],
             "box_price": round(r["box_price"], 2), "ev_net": round(r["ev_net"], 2),
             "ratio": round(r["ratio"], 3), "prob_profit": round(r["prob_profit"], 3),
             "median": round(r["median"], 2), "p10": round(r["p10"], 2), "p90": round(r["p90"], 2),
             "coverage": round(r["coverage"], 3)} for r in results]
    new = pd.DataFrame(rows, columns=RESULT_COLS)
    frames = [f for f in (old, new) if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=RESULT_COLS)
    df.to_csv(RESULTS_FILE, index=False)


def is_alert(r):
    # senza il numero di bustine confermato da MTGJSON l'EV non e' affidabile: nessun avviso
    return (r["ratio"] >= EV_ALERT_RATIO and r["coverage"] >= MIN_COVERAGE
            and r.get("packs_source") == "mtgjson")


def format_line(r):
    return (f"- {r['set']} - {r['product']}: box {r['box_price']:.2f} EUR, EV netto {r['ev_net']:.2f} EUR "
            f"(x{r['ratio']:.2f}, {r['packs']} bustine)\n    probabilita' di andare in pari {r['prob_profit'] * 100:.0f}%, "
            f"mediana {r['median']:.0f} EUR (da {r['p10']:.0f} a {r['p90']:.0f} nel 80% dei casi), "
            f"carte con prezzo noto {r['coverage'] * 100:.0f}%")
