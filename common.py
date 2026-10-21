"""Funzioni condivise dai vari moduli del bot (rete, Telegram, file di supporto)."""
import json
import os
import statistics
import time

import pandas as pd
import requests

CT_API = "https://api.cardtrader.com/api/v2"
MAGIC_GAME_ID = 1
USER_AGENT = "MTGPriceBot/2.0 (progetto personale)"


def env(name):
    return os.environ[name].strip()


# ------------------------------------------------------------------ HTTP
def _request(method, url, retries=4, timeout=60, **kwargs):
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=timeout, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt * 2)
                continue
            return r
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Richiesta a {url} fallita: {last_error}")


def ct_get(path, params=None, timeout=60):
    """Chiamata all'API CardTrader."""
    r = _request(
        "GET", f"{CT_API}{path}",
        headers={"Authorization": f"Bearer {env('CARDTRADER_TOKEN')}"},
        params=params, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def web_get_json(url, params=None, timeout=60, allow_404=False):
    """Chiamata generica a un'API pubblica (Scryfall, MTGJSON, wiki)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    r = _request("GET", url, headers=headers, params=params, timeout=timeout)
    if r.status_code == 404 and allow_404:
        return None
    r.raise_for_status()
    return r.json()


_SETS_CACHE = {}


def scryfall_sets():
    """Elenco di tutti i set (anche futuri) da Scryfall, scaricato una sola volta per esecuzione."""
    if "sets" not in _SETS_CACHE:
        data = web_get_json("https://api.scryfall.com/sets")
        _SETS_CACHE["sets"] = data.get("data", [])
    return _SETS_CACHE["sets"]


# -------------------------------------------------------------- Telegram
def send_telegram(text):
    """Invia un messaggio, spezzandolo per righe se troppo lungo."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
            data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print("Telegram ha risposto con un errore:", r.status_code, r.text[:200])


# ---------------------------------------------------------------- Prezzi
def reference_price(listings, my_user_id=None, top=3, check_condition=True):
    """
    Prezzo di riferimento = mediana delle `top` offerte piu' economiche
    (Near Mint/Mint, escluse le tue, i venditori in vacanza, le carte gradate e i bundle).
    """
    prices = []
    for lst in listings or []:
        if lst.get("on_vacation") or lst.get("graded"):
            continue
        if (lst.get("bundle_size") or 1) != 1:
            continue
        if my_user_id is not None and (lst.get("user") or {}).get("id") == my_user_id:
            continue
        if check_condition:
            condition = (lst.get("properties_hash") or {}).get("condition")
            if condition not in ("Near Mint", "Mint"):
                continue
        try:
            prices.append(lst["price"]["cents"] / 100)
        except (KeyError, TypeError):
            continue
    if not prices:
        return None
    prices.sort()
    return {"market": statistics.median(prices[:top]), "min": prices[0], "n": len(prices)}


# ------------------------------------------------------- File di supporto
def load_csv(path, columns):
    """Legge un CSV (tutto come testo). Se non esiste restituisce una tabella vuota."""
    if os.path.exists(path):
        return pd.read_csv(path, dtype=str)
    return pd.DataFrame(columns=columns)


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def recent_alerts(log, today, days):
    """Insieme di (tipo, chiave) gia' notificati negli ultimi `days` giorni."""
    if log.empty:
        return set()
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=days)).date().isoformat()
    r = log[log["date"] >= cutoff]
    return set(zip(r["type"], r["key"]))
