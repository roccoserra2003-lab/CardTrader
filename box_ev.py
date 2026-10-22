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
    {"label": "Play Booster Box", "booster_key": "play", "packs": 36, "name_words": ["play", "booster"], "plain_fallback": True},
    {"label": "Collector Booster Box", "booster_key": "collector", "packs": 12, "name_words": ["collector", "booster"]},
]
SET_TYPES = {"expansion", "core", "masters", "draft_innovation"}
MONTHS_BACK = 12              # set usciti negli ultimi 12 mesi
MIN_CARD_VALUE = 0.30         # sotto questo prezzo (EUR) una carta vale 0: nella pratica non si vende
SELL_FEE_PCT = 5.0            # commissione di vendita stimata (da verificare sul tuo account CardTrader)
EV_ALERT_RATIO = 1.10         # avvisa se EV netto / prezzo box >= 1,10
MIN_COVERAGE = 0.90           # quota minima di carte con prezzo noto, altrimenti non avvisa
USE_LOWEST_PRICE = True       # True: carte e box valutati all'offerta piu' bassa (False: mediana delle 3 piu' basse)
OUTLIER_RATIO = 0.75          # se l'offerta piu' bassa e' oltre il 25% sotto la seconda, la considero un errore
THIN_OFFERS = 3               # sotto 3 offerte il prezzo di una carta e' poco affidabile
THIN_MARKET_FACTORS = {1: 0.5, 2: 0.75}   # sconto di liquidita' per carte con 1 o 2 offerte (metti 1.0 per toglierlo)
SIMULATIONS = 5000            # box simulati per ogni prodotto
# =====================================================================

SKIP_COMPANION_TYPES = {"token", "memorabilia", "minigame", "vanguard", "alchemy", "promo",
                        "funny", "treasure_chest", "arsenal", "planechase", "archenemy"}
COMPANION_DAYS = 3            # set usciti entro 3 giorni dal set principale (Commander, bonus, ecc.)
MAX_RELATED = 6               # massimo di espansioni CardTrader correlate per nome
BASIC_LANDS = {"plains", "island", "swamp", "mountain", "forest", "wastes"}
EXCLUDE_WORDS = ("case", "bundle", "sample", "prerelease", "japan")
RESULTS_FILE = "data/box_ev.csv"
RESULT_COLS = ["date", "set_code", "set_name", "product", "box_price", "ev_net", "ratio",
               "prob_profit", "median", "p10", "p90", "coverage", "ratio_median"]


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
    return [e for e in exps if key in e["name"].lower() and e["id"] != exp_id][:MAX_RELATED]


def companion_sets(main):
    """Set usciti nello stesso periodo (Commander, bonus, archivi) da cui possono arrivare carte nelle bustine."""
    out = []
    for s in C.scryfall_sets():
        if s.get("digital") or not s.get("released_at") or s["code"] == main["code"]:
            continue
        if s.get("set_type") in SKIP_COMPANION_TYPES:
            continue
        if abs((date.fromisoformat(s["released_at"]) - main["released"]).days) <= COMPANION_DAYS:
            out.append(s)
    return out[:6]


def _is_basic_land(name):
    return str(name).lower().replace("snow-covered ", "").strip() in BASIC_LANDS


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
                    if all(w in str(x.get("name", "")).lower() for w in words))
        if count > 0:
            return count, "mtgjson"
    return default, "predefinito"


def load_mtgjson_set(code):
    name = code.upper()
    if name in {"CON", "PRN", "AUX", "NUL"}:      # nomi riservati su Windows: MTGJSON aggiunge "_"
        name += "_"
    data = C.web_get_json(f"https://mtgjson.com/api/v5/{name}.json", timeout=180, allow_404=True)
    return data.get("data") if data else None


def make_ref(listings, check_condition=True):
    """Prezzi di riferimento di una carta: mediana delle 3 piu' basse, minimo protetto da errori, n. offerte."""
    base = C.reference_price(listings, check_condition=check_condition)
    if not base:
        return None
    prices = []
    for lst in listings or []:
        if lst.get("on_vacation") or lst.get("graded") or (lst.get("bundle_size") or 1) != 1:
            continue
        if check_condition and (lst.get("properties_hash") or {}).get("condition") not in ("Near Mint", "Mint"):
            continue
        try:
            prices.append(lst["price"]["cents"] / 100)
        except (KeyError, TypeError):
            continue
    prices.sort()
    low = prices[0]
    if len(prices) >= 2 and prices[0] < OUTLIER_RATIO * prices[1]:
        low = prices[1]                       # offerta piu' bassa sospetta: uso la seconda
    base["low"] = low
    base["second"] = prices[1] if len(prices) > 1 else None
    return base


def expansion_prices(expansion_id, foil):
    """Prezzo di riferimento di ogni carta del set (inglese, Near Mint), una sola chiamata."""
    data = C.ct_get("/marketplace/products", {
        "expansion_id": expansion_id, "foil": "true" if foil else "false", "language": "en"}, timeout=240)
    time.sleep(1.1)
    prices = {}
    if isinstance(data, dict):
        for bp, listings in data.items():
            ref = make_ref(listings)
            if ref:
                prices[int(bp)] = ref          # {"market", "min", "low", "n"}
    return prices


def find_box_blueprint(blueprints, words, plain_fallback=False):
    """
    Cerca il box per nome. Per il Play Booster, se CardTrader lo chiama semplicemente
    "<Set> Booster Box" (senza "Play"), lo accetta solo se e' l'unico candidato.
    """
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
    if best is None and plain_fallback:
        plain = []
        for b in blueprints:
            n = str(b.get("name", "")).lower()
            if not ("booster box" in n or "booster display" in n):
                continue
            if any(x in n for x in EXCLUDE_WORDS + ("collector", "jumpstart", "set booster", "draft", "commander")):
                continue
            plain.append(b)
        if len(plain) == 1:
            best = plain[0]
    return best


def box_price(blueprint_id):
    for params in ({"blueprint_id": blueprint_id, "language": "en"}, {"blueprint_id": blueprint_id}):
        data = C.ct_get("/marketplace/products", params)
        time.sleep(1.1)
        listings = data.get(str(blueprint_id), []) if isinstance(data, dict) else []
        ref = make_ref(listings, check_condition=False)
        if ref:
            return ref
    return None


# ----------------------------------------------------------- Modello e EV
def build_model(booster_cfg, uuid2sf, sf2bp, prices_nf, prices_f, uuid2name=None, robust=True):
    """Trasforma la configurazione MTGJSON + i prezzi in un modello calcolabile."""
    uuid2name = uuid2name or {}
    keep = 1 - SELL_FEE_PCT / 100
    sheets = {}
    for name, sh in (booster_cfg.get("sheets") or {}).items():
        cards = sh.get("cards") or {}
        is_foil = bool(sh.get("foil"))
        weights, values, covered, gross, counts = [], [], [], [], []
        why = {"ok": 0.0, "altro set": 0.0, "senza blueprint": 0.0, "senza offerte": 0.0}
        missing = []
        for uuid, w in cards.items():
            sf = uuid2sf.get(uuid)
            price, cat, n_list = None, "ok", 0
            if sf is None:
                cat = "altro set"
            else:
                bp = sf2bp.get(sf)
                if bp is None:
                    cat = "senza blueprint"
                else:
                    ref = (prices_f.get(bp) or prices_nf.get(bp)) if is_foil else prices_nf.get(bp)
                    if ref is None:
                        cat = "senza offerte"
                    else:
                        n_list = ref["n"]
                        price = ref["low"] if robust else ref["market"]
            if price is None and cat in ("senza blueprint", "senza offerte") and _is_basic_land(uuid2name.get(uuid, "")):
                cat, price = "ok", 0.0          # le terre base valgono ~0: non contano come "mancanti"
            why[cat] += float(w)
            if cat != "ok":
                missing.append((float(w), f"{uuid2name.get(uuid, 'carta di un altro set')} [{cat}]"))
            covered.append(price is not None)
            p = price or 0.0
            gross.append(p)
            counts.append(n_list)
            if robust and n_list:
                p *= THIN_MARKET_FACTORS.get(n_list, 1.0)      # sconto di liquidita' (1 o 2 offerte)
            values.append(0.0 if p < MIN_CARD_VALUE else p * keep)
            weights.append(float(w))
        if not weights or sum(weights) <= 0:
            continue
        w_arr, v_arr = np.array(weights), np.array(values)
        total_w = w_arr.sum()
        missing.sort(reverse=True)
        sheets[name] = {
            "values": v_arr,
            "weights": w_arr,
            "uuids": list(cards.keys()),
            "gross": gross,
            "n_list": counts,
            "cdf": np.cumsum(w_arr),
            "mean": float((w_arr * v_arr).sum() / total_w),
            "cov": float((w_arr * np.array(covered)).sum() / total_w),
            "fixed": bool(sh.get("fixed")),
            "fixed_sum": float(v_arr.sum()),
            "why": {k: v / total_w for k, v in why.items()},
            "examples": [m[1] for m in missing[:3]],
        }
    boosters = booster_cfg.get("boosters") or []
    if isinstance(boosters, dict):
        boosters = [b for lst in boosters.values() for b in lst]
    configs = [{"contents": b.get("contents") or {}, "weight": float(b.get("weight", 1))} for b in boosters]
    configs = [c for c in configs if c["weight"] > 0]
    return {"sheets": sheets, "configs": configs}


def coverage_report(model):
    """Spiega perche' mancano dei prezzi: (riepilogo, dettagli per i fogli peggiori)."""
    configs, sheets = model["configs"], model["sheets"]
    total_w = sum(c["weight"] for c in configs) or 1.0
    draws = {}
    for c in configs:
        for sname, count in c["contents"].items():
            if sname in sheets and not sheets[sname]["fixed"]:
                draws[sname] = draws.get(sname, 0.0) + c["weight"] / total_w * count
    tot = sum(draws.values()) or 1.0
    agg = {}
    for sname, d in draws.items():
        for cat, share in sheets[sname]["why"].items():
            agg[cat] = agg.get(cat, 0.0) + d * share / tot
    summary = ", ".join(f"{cat} {v * 100:.0f}%" for cat, v in sorted(agg.items(), key=lambda x: -x[1])
                        if cat != "ok" and v >= 0.01)
    worst = sorted(draws, key=lambda x: -(draws[x] * (1 - sheets[x]["why"]["ok"])))[:2]
    details = []
    for sname in worst:
        miss = 1 - sheets[sname]["why"]["ok"]
        if miss >= 0.05:
            details.append(f"foglio '{sname}' ({draws[sname]:.1f} carte a bustina) manca il {miss * 100:.0f}%, "
                           f"es.: {'; '.join(sheets[sname]['examples'])}")
    return summary, details


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


def top_contributors(model, packs, uuid2name, n=5):
    """Le carte che pesano di piu' sull'EV del box: [(nome, prezzo, n. offerte, quota EV)], quota delle prime n."""
    configs, sheets = model["configs"], model["sheets"]
    total_w = sum(c["weight"] for c in configs) or 1.0
    draws = {}
    for c in configs:
        for sname, count in c["contents"].items():
            if sname in sheets and not sheets[sname]["fixed"]:
                draws[sname] = draws.get(sname, 0.0) + c["weight"] / total_w * count
    contrib = {}
    for sname, d in draws.items():
        sh = sheets[sname]
        tw = float(sh["weights"].sum())
        for i, uuid in enumerate(sh["uuids"]):
            e = contrib.setdefault(uuid, {"ev": 0.0, "price": sh["gross"][i], "n": sh["n_list"][i]})
            e["ev"] += packs * d * float(sh["weights"][i]) / tw * float(sh["values"][i])
    total = sum(v["ev"] for v in contrib.values())
    if total <= 0:
        return [], 0.0, 0.0
    ranked = sorted(contrib.items(), key=lambda kv: -kv[1]["ev"])[:n]
    top = [(uuid2name.get(u, "carta di un altro set"), v["price"], v["n"], v["ev"] / total) for u, v in ranked]
    thin = sum(v["ev"] for v in contrib.values() if 0 < v["n"] < THIN_OFFERS) / total
    return top, sum(t[3] for t in top), thin


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
def _blueprints(exp_id, cache, notes, label):
    if exp_id not in cache:
        try:
            cache[exp_id] = C.ct_get("/blueprints/export", {"expansion_id": exp_id}, timeout=120)
        except Exception as e:
            notes.append(f"{label}: blueprint non letti ({e})")
            cache[exp_id] = []
        time.sleep(0.5)
    return cache[exp_id]


def _prices(exp_id, foil, cache, notes, label):
    key = (exp_id, foil)
    if key not in cache:
        try:
            cache[key] = expansion_prices(exp_id, foil)
        except Exception as e:
            notes.append(f"{label}: prezzi non letti ({e})")
            cache[key] = {}
    return cache[key]


def evaluate_all(today, notes):
    results = []
    rng = np.random.default_rng(int(today.strftime("%Y%m%d")))
    by_code, by_name, all_exps = ct_expansion_index()
    exp_names = {e["id"]: e["name"] for e in all_exps}
    bp_cache, price_cache, set_cache = {}, {}, {}
    for s in recent_sets(today):
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

            # carte (uuid -> ID Scryfall) del set e dei set usciti lo stesso periodo
            uuid2sf, uuid2name = {}, {}

            def add_cards(data):
                for c in (data or {}).get("cards", []):
                    uuid2sf[c["uuid"]] = (c.get("identifiers") or {}).get("scryfallId")
                    uuid2name[c["uuid"]] = c.get("name", "?")

            add_cards(set_data)
            exp_ids = [exp_id]
            for cs in companion_sets(s):
                if cs["code"] not in set_cache:
                    try:
                        set_cache[cs["code"]] = load_mtgjson_set(cs["code"])
                    except Exception as e:
                        set_cache[cs["code"]] = None
                        notes.append(f"{s['name']}: set collegato {cs['code']} non letto ({e})")
                add_cards(set_cache[cs["code"]])
                cid = by_code.get(cs["code"].lower()) or by_name.get(cs["name"].lower())
                if cid and cid not in exp_ids:
                    exp_ids.append(cid)
            for e in related_expansions(s["name"], exp_id, all_exps):
                if e["id"] not in exp_ids:
                    exp_ids.append(e["id"])

            # blueprint e prezzi di tutte le espansioni CardTrader coinvolte
            blueprints, prices_nf, prices_f = [], {}, {}
            for eid in exp_ids:
                label = f"{s['name']} / {exp_names.get(eid, eid)}"
                blueprints += _blueprints(eid, bp_cache, notes, label)
                prices_nf.update(_prices(eid, False, price_cache, notes, label))
                prices_f.update(_prices(eid, True, price_cache, notes, label))
            sf2bp = {b["scryfall_id"]: b["id"] for b in blueprints if b.get("scryfall_id")}
            used = ", ".join(exp_names.get(e, str(e)) for e in exp_ids)

            for p in products:
                box_bp = find_box_blueprint(blueprints, p["name_words"], p.get("plain_fallback", False))
                if not box_bp:
                    seen = box_like_names(blueprints) or ["nessuno"]
                    notes.append(f"{s['name']}: {p['label']} non trovato su CardTrader. "
                                 f"Prodotti simili: {'; '.join(seen)}")
                    continue
                if p.get("plain_fallback") and "play" not in str(box_bp.get("name", "")).lower():
                    notes.append(f"{s['name']}: per {p['label']} uso '{box_bp.get('name')}'.")
                ref = box_price(box_bp["id"])
                if not ref:
                    notes.append(f"{s['name']}: nessuna offerta per {p['label']}.")
                    continue
                model = build_model(booster[p["booster_key"]], uuid2sf, sf2bp, prices_nf, prices_f, uuid2name,
                                    robust=USE_LOWEST_PRICE)
                if not model["sheets"] or not model["configs"]:
                    notes.append(f"{s['name']}: struttura bustina non utilizzabile per {p['label']}.")
                    continue
                packs, packs_source = packs_per_box(set_data, p["name_words"], p["packs"])
                price_used = ref["min"] if USE_LOWEST_PRICE else ref["market"]   # box: offerta piu' bassa reale, senza protezione
                metrics = evaluate_product(model, packs, price_used, rng)
                old_model = build_model(booster[p["booster_key"]], uuid2sf, sf2bp, prices_nf, prices_f, uuid2name,
                                        robust=False)
                metrics["ratio_median"] = (pack_ev_and_coverage(old_model)[0] * packs / ref["market"]
                                           if ref["market"] > 0 else 0.0)
                metrics["top"], metrics["top_share"], metrics["thin_share"] = top_contributors(model, packs, uuid2name)
                if metrics["coverage"] < MIN_COVERAGE:
                    summary, details = coverage_report(model)
                    notes.append(f"{s['name']} - {p['label']}: prezzi noti {metrics['coverage'] * 100:.0f}%, "
                                 f"mancano: {summary}. " + " | ".join(details)
                                 + f" [espansioni CardTrader usate: {used}]")
                results.append({"code": s["code"], "set": s["name"], "product": p["label"],
                                "box_price": price_used, "box_second": ref.get("second"), "packs": packs,
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
             "coverage": round(r["coverage"], 3), "ratio_median": round(r.get("ratio_median", 0), 3)} for r in results]
    new = pd.DataFrame(rows, columns=RESULT_COLS)
    frames = [f for f in (old, new) if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=RESULT_COLS)
    df.to_csv(RESULTS_FILE, index=False)


def is_alert(r):
    # senza il numero di bustine confermato da MTGJSON l'EV non e' affidabile: nessun avviso
    return (r["ratio"] >= EV_ALERT_RATIO and r["coverage"] >= MIN_COVERAGE
            and r.get("packs_source") == "mtgjson")


def format_line(r):
    text = (f"- {r['set']} - {r['product']}: box {r['box_price']:.2f} EUR, EV netto {r['ev_net']:.2f} EUR "
            f"(x{r['ratio']:.2f}, {r['packs']} bustine)\n"
            f"    probabilita' di andare in pari {r['prob_profit'] * 100:.0f}%, "
            f"mediana {r['median']:.0f} EUR (da {r['p10']:.0f} a {r['p90']:.0f} nel 80% dei casi), "
            f"carte con prezzo noto {r['coverage'] * 100:.0f}%\n"
            f"    Metodo precedente (mediana delle 3 offerte piu' basse): x{r.get('ratio_median', 0):.2f}")
    second = r.get("box_second")
    if second and r["box_price"] < OUTLIER_RATIO * second:
        text += (f"\n    ATTENZIONE: l'offerta piu' bassa del box ({r['box_price']:.0f} EUR) e' "
                 f"{(1 - r['box_price'] / second) * 100:.0f}% sotto la seconda ({second:.0f} EUR): "
                 f"possibile occasione, ma controlla venditore, lingua, condizioni e che sia ancora disponibile")
    if r.get("top"):
        cards = "; ".join(f"{name} ({price:.0f} EUR, {n} offerte, {share * 100:.0f}% dell'EV)"
                          for name, price, n, share in r["top"])
        text += (f"\n    Carte che pesano di piu': {cards}"
                 f"\n    Le prime {len(r['top'])} valgono il {r['top_share'] * 100:.0f}% dell'EV; "
                 f"{r.get('thin_share', 0) * 100:.0f}% dell'EV viene da carte con meno di {THIN_OFFERS} offerte")
    return text
