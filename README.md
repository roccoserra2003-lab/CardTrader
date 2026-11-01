# CardTrader MTG Bot

Bot Telegram che ogni giorno (GitHub Actions, 06:00 UTC) controlla le carte Magic in vendita su CardTrader e ti scrive solo quando c'e' qualcosa da sapere.

## Cosa fa
- **VENDI ORA**: segnali di vendita sulle carte che possiedi (prezzo alto rispetto a mediana a 30 giorni, salita a 7 giorni, z-score, massimo a 90 giorni).
- **EV BOX**: valore atteso netto dei Play/Collector Booster Box recenti (struttura bustine da MTGJSON, prezzi CardTrader controllati con Cardmarket, simulazione di 5000 box).
- **Calendario**: uscite, rotazione Standard, finestre B&R, ban/unban reali (Scryfall), tornei (wiki).

## File
| File | Ruolo |
|---|---|
| `bot.py` | Programma principale: inventario, segnali, messaggio Telegram |
| `box_ev.py` | EV dei box e simulazione |
| `calendar_events.py` | Calendario eventi e ban |
| `mtgjson_data.py` | Prezzi Cardmarket di oggi, etichette di rischio |
| `import_history.py` | Importazione una tantum dello storico (workflow manuale) |
| `common.py` | HTTP con retry, Telegram, scritture atomiche |
| `data/` | Storico salvato dal bot (CSV/JSON) |

## Configurazione
Secrets del repository: `CARDTRADER_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Le soglie si cambiano all'inizio di `bot.py` e `box_ev.py`.

## Affidabilita'
- Se CardTrader non risponde per 5 carte di fila il bot si ferma e te lo dice (non resta bloccato per ore).
- Un errore su una singola carta non fa perdere i prezzi delle altre.
- Gli avvisi vengono segnati come "inviati" solo se Telegram li ha consegnati.
- File di dati scritti in modo atomico; due esecuzioni non possono sovrapporsi.
- Se il workflow fallisce prima/senza che il bot riesca a scriverti, parte un messaggio di emergenza.

Codici di uscita di `bot.py`: `0` ok, `1` errore grave o Telegram non consegnato, `2` errori parziali gia' elencati nel messaggio.

## Test
```
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                    # tutto (inclusi i controlli sui dati reali in data/)
pytest -q -m "not realdata"  # solo codice (e' quello che gira in CI)
```
I test non usano la rete: CardTrader, Scryfall, MTGJSON e Telegram sono simulati.
