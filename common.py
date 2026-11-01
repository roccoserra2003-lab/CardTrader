"""Funzioni condivise dai vari moduli del bot (rete, Telegram, file di supporto)."""
import json
import os
import statistics
import tempfile
import time

import pandas as pd
import requests

CT_API = "https://api.cardtrader.com/api/v2"
MAGIC_GAME_ID = 1
USER_AGENT = "MTGPriceBot/2.0 (progetto personale)"


def env(name):
    try:
        return os.environ[name].strip()
    except KeyError:
        raise RuntimeError(f"Variabile d'ambiente mancante: {name} "
                           f"(controlla i Secrets del repository su GitHub)") from None


# ------------------------------------------------------------------ HTTP
def _retry_after(r, default):
    """Secondi da attendere: rispetta l'intestazione Retry-After (max 60 s), altrimenti `default`."""
    try:
        return min(60.0, max(0.0, float(getattr(r, "headers", {}).get("Retry-After", default))))
    except (TypeError, ValueError):
        return default


def _request(method, url, retries=4, timeout=60, **kwargs):
    last_error = None
    for attempt in range(retries):
        wait = 2 ** attempt * 2
        try:
            r = requests.request(method, url, timeout=timeout, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                wait = _retry_after(r, wait)
            else:
                return r
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < retries - 1:               # niente attesa inutile dopo l'ultimo tentativo
            time.sleep(wait)
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
TELEGRAM_LIMIT = 4000          # Telegram accetta al massimo 4096 caratteri per messaggio


def split_message(text, limit=TELEGRAM_LIMIT):
    """Spezza il testo per righe in blocchi <= limit; una riga enorme viene tagliata. Mai blocchi vuoti."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _telegram_retry_after(r, default):
    try:
        return min(60.0, float(r.json().get("parameters", {}).get("retry_after", default)))
    except Exception:
        return default


def send_telegram(text, attempts=3):
    """
    Invia un messaggio (spezzato se lungo). Ritorna True solo se TUTTI i pezzi sono arrivati.
    Riprova su errori di rete, 429 (rispettando retry_after) e 5xx; non riprova su errori 4xx (token/chat errati).
    """
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    delivered = True
    for chunk in split_message(text):
        ok = False
        for attempt in range(attempts):
            wait = 2 ** attempt
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  data={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
                                  timeout=30)
            except requests.RequestException as e:
                print("Telegram non raggiungibile:", str(e).replace(token, "***")[:200])
            else:
                if r.ok:
                    ok = True
                    break
                print("Telegram ha risposto con un errore:", r.status_code, r.text[:200])
                if r.status_code == 429:
                    wait = _telegram_retry_after(r, wait)
                elif r.status_code < 500:
                    break                      # errore definitivo (token o chat sbagliati): inutile riprovare
            if attempt < attempts - 1:
                time.sleep(wait)
        delivered = delivered and ok
    return delivered


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


def _atomic_write(path, writer):
    """Scrive su un file temporaneo e lo rinomina: un crash a meta' scrittura non corrompe il file vero."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp_", suffix=os.path.splitext(path)[1])
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer(f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def save_json(path, data):
    _atomic_write(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=1))


def save_csv(df, path):
    """to_csv(index=False) in modo atomico."""
    _atomic_write(path, lambda f: df.to_csv(f, index=False))


def recent_alerts(log, today, days):
    """Insieme di (tipo, chiave) gia' notificati negli ultimi `days` giorni."""
    if log.empty:
        return set()
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=days)).date().isoformat()
    r = log[log["date"] >= cutoff]
    return set(zip(r["type"], r["key"]))
