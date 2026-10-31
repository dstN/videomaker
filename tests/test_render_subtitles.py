from app.services.render_service import _write_ass_subtitles

WORDS = [
    {"word": "Er", "start": 0.0, "end": 0.3},
    {"word": "kam{\\b1}", "start": 0.3, "end": 0.8},
    {"word": "zurück", "start": 0.0, "end": 0.0},  # unaligned last word
]


def _dialogues(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("Dialogue:")]


def test_german_labels_and_escaping(tmp_path):
    ass = tmp_path / "p.ass"
    _write_ass_subtitles(WORDS, ass, 1, 3, "Der {Turm}\\N 22", 12.0, 8.0, "de")
    lines = _dialogues(ass)
    text = "\n".join(lines)

    assert "TEIL 1 / 3" in text
    assert "Folgen für Teil 2" in text
    assert "DER (TURM)/N 22" in text
    assert "KAM(/B1)" in text
    # Only the \pos override blocks may contain braces
    for line in lines:
        payload = line.split(",,", 1)[1].split("}", 1)[1]
        assert "{" not in payload and "}" not in payload


def test_cta_uses_wav_duration_when_last_word_unaligned(tmp_path):
    ass = tmp_path / "p.ass"
    _write_ass_subtitles(WORDS, ass, 1, 2, "", 12.0, 8.0, "en")
    cta = [l for l in _dialogues(ass) if "Follow for Part 2" in l]
    assert cta and cta[0].split(",")[1] == "0:00:05.00"  # 8.0 s audio - 3 s


def test_unknown_language_falls_back_to_english(tmp_path):
    ass = tmp_path / "p.ass"
    _write_ass_subtitles(WORDS, ass, 2, 2, "", 12.0, 8.0, "xx")
    assert "PART 2 / 2" in ass.read_text(encoding="utf-8")


def test_render_refuses_audio_over_video_limit(tmp_path, monkeypatch):
    import io
    import json
    import wave

    import pytest

    from app.services import render_service

    audio = tmp_path / "a.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * 181)
    ts = tmp_path / "t.json"
    ts.write_text(json.dumps(WORDS))
    bg = tmp_path / "bg"
    bg.mkdir()
    (bg / "b.mp4").write_bytes(b"x")
    monkeypatch.setattr(render_service, "_pick_random", lambda d, e: bg / "b.mp4")

    with pytest.raises(RuntimeError, match="over the 180s video limit"):
        render_service.render_part(1, 1, 1, "", audio, ts, tmp_path / "o.mp4")
