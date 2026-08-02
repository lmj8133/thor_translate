"""Glossary parsing and matching unit tests."""

import os

from server.proxy.glossary import Glossary, GlossaryEntry, parse_glossary


def test_parse_note_optional_and_comments():
    text = (
        "# comment line\n"
        "// another comment\n"
        "\n"
        "ポケモン->寶可夢\n"
        "ダイゴ->大吾 #人名，男性\n"
        "malformed line without arrow\n"
        "->empty-src\n"
        "ダイゴ->重複 #duplicate src, skipped\n"
    )
    entries = parse_glossary(text)
    assert entries == [
        GlossaryEntry("ポケモン", "寶可夢"),
        GlossaryEntry("ダイゴ", "大吾", "人名，男性"),
    ]
    assert entries[0].to_line() == "ポケモン->寶可夢"
    assert entries[1].to_line() == "ダイゴ->大吾 #人名，男性"


def test_match_orders_by_first_occurrence():
    entries = parse_glossary("ポケモン->寶可夢\nダイゴ->大吾 #人名\n")
    glossary = Glossary(None)
    glossary._entries = entries
    hits = glossary.match("ダイゴさんはポケモンずかんを　もっている")
    assert [entry.dst for entry in hits] == ["大吾", "寶可夢"]


def test_empty_glossary_matches_nothing():
    assert Glossary(None).match("ダイゴさん") == []


def test_hot_reload_on_mtime_change(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text("ダイゴ->大吾\n", encoding="utf-8")
    glossary = Glossary(path)
    assert [entry.dst for entry in glossary.match("ダイゴとポケモン")] == ["大吾"]

    path.write_text("ダイゴ->大吾\nポケモン->寶可夢\n", encoding="utf-8")
    # Force a different mtime; some filesystems have coarse timestamp granularity.
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    assert [entry.dst for entry in glossary.match("ダイゴとポケモン")] == ["大吾", "寶可夢"]


def test_reload_recovers_from_truncate_then_write(tmp_path):
    # Non-atomic writers truncate first; a request may read the empty file in
    # between. The size-aware fingerprint must pick up the completed write even
    # if it lands within the same mtime tick.
    path = tmp_path / "glossary.txt"
    path.write_text("ダイゴ->大吾\n", encoding="utf-8")
    glossary = Glossary(path)
    with path.open("w", encoding="utf-8") as handle:
        assert glossary.match("ダイゴとポケモン") == []
        handle.write("ダイゴ->大吾\nポケモン->寶可夢\n")
    assert [entry.dst for entry in glossary.match("ダイゴとポケモン")] == ["大吾", "寶可夢"]


def test_file_disappearing_keeps_last_entries(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text("ダイゴ->大吾\n", encoding="utf-8")
    glossary = Glossary(path)
    path.unlink()
    assert [entry.dst for entry in glossary.match("ダイゴさん")] == ["大吾"]
