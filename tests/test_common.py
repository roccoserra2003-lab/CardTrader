import pandas as pd
import pytest
import requests

import common as C
from conftest import listing


# ------------------------------------------------------------ reference_price
def test_reference_price_median_of_three_cheapest():
    ls = [listing(p) for p in (5, 1, 2, 3, 4)]
    r = C.reference_price(ls)
    assert r == {"market": 2, "min": 1, "n": 5}


def test_reference_price_filters():
    ls = [
        listing(1.0, on_vacation=True),            # venditore in vacanza
        listing(1.1, graded=True),                 # gradata
        listing(1.2, bundle_size=4),               # bundle
        listing(1.3, cond="Played"),               # condizione
        listing(1.4, user=99),                     # mia
        listing(2.0),
        listing(3.0),
    ]
    r = C.reference_price(ls, my_user_id=99)
    assert r["n"] == 2 and r["min"] == 2.0 and r["market"] == 2.5


def test_reference_price_no_condition_check_and_empty():
    assert C.reference_price([listing(1, cond="Played")], check_condition=False)["min"] == 1
    assert C.reference_price([]) is None
    assert C.reference_price(None) is None
    assert C.reference_price([{"price": None}]) is None


# ----------------------------------------------------------------------- HTTP
class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload or {}
        self.ok = status < 400
        self.text = "boom"

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_request_retries_then_succeeds(monkeypatch):
    seq = [FakeResp(500), FakeResp(429), FakeResp(200, {"ok": 1})]
    calls = []
    monkeypatch.setattr(requests, "request", lambda *a, **k: calls.append(1) or seq.pop(0))
    assert C._request("GET", "http://x").json() == {"ok": 1}
    assert len(calls) == 3


def test_request_gives_up(monkeypatch):
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResp(503))
    with pytest.raises(RuntimeError, match="fallita"):
        C._request("GET", "http://x", retries=3)


def test_request_network_exception_retried(monkeypatch):
    seq = [requests.ConnectionError("down"), FakeResp(200, {"a": 1})]

    def fake(*a, **k):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(requests, "request", fake)
    assert C._request("GET", "http://x").json() == {"a": 1}


def test_web_get_json_404(monkeypatch):
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResp(404))
    assert C.web_get_json("http://x", allow_404=True) is None
    with pytest.raises(requests.HTTPError):
        C.web_get_json("http://x")


def test_ct_get_sends_bearer(monkeypatch, env):
    seen = {}

    def fake(method, url, **kw):
        seen.update(kw, url=url)
        return FakeResp(200, [1])
    monkeypatch.setattr(requests, "request", fake)
    assert C.ct_get("/expansions") == [1]
    assert seen["headers"]["Authorization"] == "Bearer x"
    assert seen["url"].endswith("/api/v2/expansions")


def test_env_strips_whitespace(monkeypatch):
    monkeypatch.setenv("FOO", "  abc\n")
    assert C.env("FOO") == "abc"


# ------------------------------------------------------------------- Telegram
def _capture_post(monkeypatch):
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, data=None, timeout=None: sent.append(data["text"]) or FakeResp(200))
    return sent


def test_send_telegram_splits_long_messages(monkeypatch, env):
    sent = _capture_post(monkeypatch)
    text = "\n".join(f"riga numero {i} " + "x" * 80 for i in range(200))
    C.send_telegram(text)
    assert len(sent) > 1
    assert all(0 < len(s) <= 4096 for s in sent)
    assert "".join(sent).replace("\n", "") == text.replace("\n", "")


def test_send_telegram_short_message(monkeypatch, env):
    sent = _capture_post(monkeypatch)
    C.send_telegram("ciao")
    assert sent == ["ciao\n"]


# ------------------------------------------------------------- file di supporto
def test_load_csv_missing_and_existing(tmp_path):
    df = C.load_csv(str(tmp_path / "no.csv"), ["a", "b"])
    assert list(df.columns) == ["a", "b"] and df.empty
    p = tmp_path / "x.csv"
    pd.DataFrame({"a": [1, 2]}).to_csv(p, index=False)
    assert C.load_csv(str(p), ["a"])["a"].tolist() == ["1", "2"]     # tutto come testo


def test_json_roundtrip(tmp_path):
    p = str(tmp_path / "a.json")
    assert C.load_json(p, {"d": 1}) == {"d": 1}
    C.save_json(p, {"è": [1, 2]})
    assert C.load_json(p) == {"è": [1, 2]}


def test_recent_alerts():
    log = pd.DataFrame({"date": ["2026-10-01", "2026-10-20", "2026-10-25"],
                        "type": ["sell", "sell", "box"], "key": ["a", "b", "c"]})
    assert C.recent_alerts(log, __import__("datetime").date(2026, 10, 26), 5) == {("box", "c")}
    assert C.recent_alerts(log, __import__("datetime").date(2026, 10, 26), 7) == {("box", "c"), ("sell", "b")}
    assert C.recent_alerts(log, __import__("datetime").date(2026, 10, 26), 30) == {
        ("sell", "a"), ("sell", "b"), ("box", "c")}
    assert C.recent_alerts(pd.DataFrame(columns=["date", "type", "key"]), __import__("datetime").date(2026, 10, 26), 7) == set()
