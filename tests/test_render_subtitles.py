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
