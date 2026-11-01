import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Niente attese reali (il bot aspetta ~1 secondo tra le chiamate API)."""
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Cartella di lavoro isolata con data/ vuota: i test non toccano i file veri."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CARDTRADER_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")


def listing(price, cond="Near Mint", user=1, **kw):
    d = {"price": {"cents": int(round(price * 100))}, "properties_hash": {"condition": cond},
         "user": {"id": user}}
    d.update(kw)
    return d
