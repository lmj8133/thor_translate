"""Bulk-build the Pokémon glossary from 52poke wiki list pages.

Fetches the zh-hant variant of the big list pages (Pokémon national dex,
moves, abilities, items, game NPCs) plus the Hoenn town pages, extracts
(Japanese, Taiwan-Traditional) name pairs, and regenerates the auto section
of glossaries/pokemon-oras.txt. The hand-curated section above the marker
line is preserved verbatim and always wins on duplicate terms.

Usage:
    uv run python tools/build_glossary.py                 # uses cached pages
    uv run python tools/build_glossary.py --refresh       # refetch everything

Pages are cached in --cache-dir (default ~/.cache/52poke) and fetched with a
browser User-Agent at a polite interval; a full refresh is ~25 requests.
Exit codes: 0 success, 1 on fetch/parse failure.
"""

import argparse
import html
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://wiki.52poke.com/zh-hant/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FETCH_INTERVAL_S = 1.5

# key -> (page title, category note in Simplified — model-facing convention)
LIST_PAGES = {
    "pokemon": ("寶可夢列表（按全國圖鑑編號）", "宝可梦"),
    "moves": ("招式列表", "招式"),
    "abilities": ("特性列表", "特性"),
    "items": ("道具列表", "道具"),
    "npcs": ("遊戲人物列表", "人名"),
}
HOENN_PAGE = "豐緣地區"

MARKER = "# ===== AUTO-GENERATED BELOW (tools/build_glossary.py) — hand edits above ====="

# A zh-name cell (a text link) immediately followed by the Japanese-name cell.
PAIR_ROW = re.compile(
    r'<td[^>]*><a href="[^"]*" title="[^"]*">([^<]+)</a>\s*\n</td>\s*\n<td[^>]*>([^<\n]+)\n'
)
KANA = re.compile(r"[぀-ヿ]")
HAN = re.compile(r"[一-鿿]")
# First Japanese-tagged span/cell on a town page (infobox name row).
TOWN_JA = re.compile(r'lang="ja"[^>]*>\s*([^<\n]+?)\s*<')

# Source-data corrections, found by the 2026-08-03 12-agent semantic audit
# and adjudicated against the cached wiki HTML. Keys are the ja terms as they
# appear ON THE WIKI; fixes repair wiki-side typos, drops remove entries whose
# Japanese was corrupted by the wiki's zh-hant LanguageConverter (kanji inside
# ja text gets Traditionalized and can then never match real game text).
SRC_FIXES = {
    "みずたまりボン": "みずたまリボン",  # hiragana り typo inside a katakana word
    "リプテラナイト": "プテラナイト",    # stray leading リ; the Mega Stone is プテラ+ナイト
}
SRC_DROPS = {
    "ゼロの秘寶",  # ja 宝 corrupted to 寶 by the variant converter; SV DLC title anyway
}

# Type names are battle-critical, tiny, and stable; kept static rather than
# scraped (エスパー→超能力 and friends are the non-obvious ones).
STATIC_TYPES = [
    ("ノーマル", "一般"), ("ほのお", "火"), ("みず", "水"), ("でんき", "電"),
    ("くさ", "草"), ("こおり", "冰"), ("かくとう", "格鬥"), ("どく", "毒"),
    ("じめん", "地面"), ("ひこう", "飛行"), ("エスパー", "超能力"), ("むし", "蟲"),
    ("いわ", "岩石"), ("ゴースト", "幽靈"), ("ドラゴン", "龍"), ("あく", "惡"),
    ("はがね", "鋼"), ("フェアリー", "妖精"),
]


def fetch(title: str, cache_dir: Path, cache_key: str, refresh: bool) -> str:
    cache_file = cache_dir / f"{cache_key}.html"
    if cache_file.exists() and not refresh:
        return cache_file.read_text(encoding="utf-8", errors="replace")
    url = BASE + urllib.parse.quote(title)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise SystemExit(f"Fetch failed for {title}: {exc}") from exc
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    time.sleep(FETCH_INTERVAL_S)
    return text


def clean(cell: str) -> str:
    # Strip invisible format characters (LRM etc.) - they would make an
    # otherwise-correct entry unmatchable against real game text.
    text = re.sub("[\u00ad\u200b-\u200f\u2060\ufeff]", "", html.unescape(cell))
    return text.replace(" ", " ").strip()


def extract_pairs(page_html: str) -> list[tuple[str, str]]:
    """(ja, zh) pairs from a list page; kana/han filters drop non-name rows."""
    pairs = []
    for zh_raw, ja_raw in PAIR_ROW.findall(page_html):
        zh, ja = clean(zh_raw), clean(ja_raw)
        if not zh or not ja or not KANA.search(ja) or not HAN.search(zh):
            continue
        pairs.append((ja, zh))
    return pairs


def hoenn_towns(cache_dir: Path, refresh: bool) -> list[tuple[str, str]]:
    """Town names need one fetch per town: the region page has zh only."""
    region = fetch(HOENN_PAGE, cache_dir, "hoenn", refresh)
    towns = re.findall(r'<td><a href="/wiki/[^"]*" title="([^"]+[市鎮村])">\1</a>', region)
    pairs = []
    for zh in dict.fromkeys(towns):
        page = fetch(zh, cache_dir, f"town-{zh}", refresh)
        match = TOWN_JA.search(page)
        if not match or not KANA.search(match.group(1)):
            print(f"  WARN: no Japanese name found for town {zh}, skipped")
            continue
        pairs.append((clean(match.group(1)), zh))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the auto section of the Pokémon glossary")
    parser.add_argument("--refresh", action="store_true", help="refetch pages instead of using the cache")
    parser.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "52poke"))
    parser.add_argument(
        "--glossary",
        default=str(Path(__file__).resolve().parent.parent / "glossaries" / "pokemon-oras.txt"),
    )
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    glossary_path = Path(args.glossary)

    hand_text = glossary_path.read_text(encoding="utf-8")
    if MARKER in hand_text:
        hand_text = hand_text[: hand_text.index(MARKER)].rstrip() + "\n"
    hand_srcs = {
        line.partition("->")[0].strip()
        for line in hand_text.splitlines()
        if "->" in line and not line.lstrip().startswith(("#", "//"))
    }

    sections: list[tuple[str, str, list[tuple[str, str]]]] = []
    for key, (title, note) in LIST_PAGES.items():
        pairs = extract_pairs(fetch(title, cache_dir, key, args.refresh))
        sections.append((key, note, pairs))
        print(f"{key}: {len(pairs)} pairs")
    town_pairs = hoenn_towns(cache_dir, args.refresh)
    sections.append(("towns", "地名，丰缘", town_pairs))
    print(f"towns: {len(town_pairs)} pairs")
    sections.append(("types", "属性", STATIC_TYPES))

    seen = set(hand_srcs)
    skipped_short = 0
    lines = [hand_text.rstrip(), "", MARKER]
    for key, note, pairs in sections:
        lines.append(f"# --- {key} ---")
        count = 0
        for ja, zh in pairs:
            if ja in SRC_DROPS:
                continue
            ja = SRC_FIXES.get(ja, ja)
            if ja in seen:
                continue
            if len(ja) <= 2:  # substring-matching hazard, see glossaries/README.md
                skipped_short += 1
                continue
            seen.add(ja)
            lines.append(f"{ja}->{zh} #{note}")
            count += 1
        print(f"  wrote {key}: {count}")
    glossary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    total = len(seen) - len(hand_srcs)
    print(f"Done: {total} generated entries (+{len(hand_srcs)} hand-curated), "
          f"{skipped_short} skipped as too short -> {glossary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
