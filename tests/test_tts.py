import httpx
import pytest

from app.services import tts_service
from app.services.tts_service import TTSError, spoken_text


def test_english_numbers():
    assert spoken_text("Tower 12 at 6:14 AM in 1987, 3.5 miles", "en") == (
        "Tower twelve at six fourteen am in nineteen eighty-seven, three point five miles"
    )


def test_german_numbers():
    assert spoken_text("Turm 12 um 16:05 Uhr im Jahr 1987, 3,5 Kilometer", "de") == (
        "Turm zwölf um sechzehn Uhr fünf im Jahr neunzehnhundertsiebenundachtzig, "
        "drei Komma fünf Kilometer"
    )


def test_german_full_hour_and_dot_time():
    assert spoken_text("um 8.00 Uhr", "de") == "um acht Uhr"


def test_unsupported_language_falls_back_to_english_words():
    assert spoken_text("7", "xx") == "seven"


class _Resp:
    def __init__(self, status=200, content=b"R" * 100):
        self.status_code = status
        self.content = content
        self.text = "err"


class _Client:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, *_a, **_k):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(tts_service.time, "sleep", lambda _s: None)


def test_retries_timeout_then_succeeds():
    client = _Client([httpx.ReadTimeout("t"), _Resp()])
    assert tts_service._synthesize_with_retry(client, "u", "hi", "v", 3, "chunk 1/1") == b"R" * 100
    assert client.calls == 2


def test_timeout_message_names_chunk_and_configured_timeout():
    client = _Client([httpx.ReadTimeout("t")] * 3)
    with pytest.raises(TTSError, match=r"timed out after 400s on chunk 2/5 \(5 chars, 3 attempt"):
        tts_service._synthesize_with_retry(client, "u", "hello", "v", 3, "chunk 2/5")


def test_client_errors_are_not_retried():
    client = _Client([_Resp(status=422)])
    with pytest.raises(TTSError, match="HTTP 422"):
        tts_service._synthesize_with_retry(client, "u", "hi", "v", 3, "chunk 1/1")
    assert client.calls == 1


def test_connect_error_fails_fast():
    client = _Client([httpx.ConnectError("down")])
    with pytest.raises(TTSError, match="Cannot connect"):
        tts_service._synthesize_with_retry(client, "u", "hi", "v", 3, "chunk 1/1")
    assert client.calls == 1
