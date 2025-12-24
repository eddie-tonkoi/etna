#!/usr/bin/env python3
"""## L↔R Confusion Check — what this script does

This tool looks for a specific class of errors sometimes made by multilingual
writers: accidental swaps of **l** and **r** when they appear as the *second*
consonant inside a syllable — patterns like **V C L/R V**.

It isn’t trying to guess your intent or flag everyday words. Instead, it uses a
small linguistic heuristic:

- find tokens that match a vowel → consonant → l/r → vowel pattern,
- ignore anything that’s too short, titlecase, or begins with l/r,
- and only flag the word if swapping that l/r produces a valid word
  according to your combined lexicon.

The script builds that lexicon from:

- your project dictionary (`dict_book.txt`),
- the shared custom dictionary (configured in `scripts/common/config.yaml`),
- and, if available, the system word list.

To avoid noise, it also:

- skips known-good British spellings (programme, jewellery, tyre, etc.),
- ignores tokens that are already valid words in your dictionaries,
- and allows an extra whitelist via `--allow-file` for special cases.

The output is a focused report (`r_04_lr_confusion.md`) grouped by word.

It’s deliberately conservative: if it flags something, it’s worth a look — but
it should stay mostly quiet in a clean manuscript.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re
import sys

# ---------------- Config ----------------
REPO_ROOT = Path(__file__).resolve().parents[2]

# Allow importing shared config loader from scripts/common/common.py
sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
from common import load_paths  # noqa: E402

try:
    PATHS = load_paths()
except FileNotFoundError as e:
    print(f"❌ {e}")
    print("Expected etna/scripts/common/config.yaml to exist (optionally overlaid by config.local.yaml).")
    sys.exit(1)
except ImportError as e:
    print(f"❌ {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Failed to load scripts/common/config.yaml (and optional config.local.yaml): {e}")
    sys.exit(1)


def load_dictionary_with_counts(
    path: Path,
    _seen: set[Path] | None = None,
    _counts: dict[Path, int] | None = None,
) -> tuple[set[str], dict[Path, int]]:
    """Load a wordlist dictionary with optional `#include` support, plus per-file counts.

    Rules:
    - Blank lines are ignored.
    - Lines starting with `#` are comments, except `#include <path>`.
    - `#include` paths may be absolute or relative to the including file.
    - Includes are recursive with cycle protection.

    Counts are the number of unique entries present in each file itself (excluding includes/comments).
    """
    if _seen is None:
        _seen = set()
    if _counts is None:
        _counts = {}

    resolved = path.expanduser().resolve()
    if resolved in _seen:
        return set(), _counts
    _seen.add(resolved)

    if not resolved.exists():
        print(f"❌ Dictionary file not found: {resolved}")
        sys.exit(1)

    local_words: set[str] = set()
    all_words: set[str] = set()

    with open(resolved, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("#include"):
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    print(f"❌ Invalid include directive in {resolved}: {line}")
                    sys.exit(1)

                include_str = parts[1].strip().strip('"').strip("'")
                include_path = Path(include_str)
                if not include_path.is_absolute():
                    include_path = resolved.parent / include_path

                include_resolved = include_path.expanduser().resolve()
                if not include_resolved.exists():
                    print(f"❌ Included dictionary not found: {include_resolved}")
                    print(f"Referenced from: {resolved}")
                    sys.exit(1)

                included_words, _counts = load_dictionary_with_counts(
                    include_resolved, _seen=_seen, _counts=_counts
                )
                all_words |= included_words
                continue

            if line.startswith("#"):
                continue

            w = line.strip().strip("’'\"").lower()
            if w:
                local_words.add(w)

    _counts[resolved] = len(local_words)
    all_words |= local_words

    return all_words, _counts


def load_allow_targets(path: Path) -> set[str]:
    """Load allow-list targets (mandatory if --allow-file is provided)."""
    if not path.exists():
        print(f"❌ Allow-file not found: {path.resolve()}")
        sys.exit(1)
    words, _ = load_dictionary_with_counts(path)
    return words


# ——— Heuristics ———
VOWEL = "aeiouy"
CONS = "bcdfghjkmnpqtvwxz"  # no l/r here — they’re special
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
TITLECASE_RE = re.compile(r"^[A-Z][a-z]+$")
VC_LR_V = re.compile(rf"[{VOWEL}][{CONS}][lr][{VOWEL}]", re.IGNORECASE)

KEEP_WORDS = {
    "programme",
    "jewellery",
    "kerb",
    "tyre",
    "draught",
    "honour",
    "traveller",
    "mould",
    "sombre",
    "armour",
    "rumour",
    "savour",
    "clamour",
    "labour",
    "favour",
    "flavour",
}


def is_titlecase(tok: str) -> bool:
    return bool(TITLECASE_RE.match(tok))


def internal_second_consonant_positions(word: str) -> list[int]:
    """Indices where word[i] is l/r and forms V C [lr] V, with i > 1 (not word-initial)."""
    w = word.lower()
    hits: list[int] = []
    for m in VC_LR_V.finditer(w):
        i = m.start() + 2  # V C [lr] V
        if i > 1:
            hits.append(i)
    return hits


def swap_lr_at(word: str, i: int) -> str:
    w = list(word.lower())
    w[i] = "l" if w[i] == "r" else "r"
    return "".join(w)


def run_checks(text: str, filename: str, lexicon: set[str], allow_targets: set[str]) -> list[dict]:
    issues: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in TOKEN_RE.finditer(line):
            tok = m.group(0)
            tl = tok.lower()

            # skips
            if len(tl) < 4:
                continue
            if tl in KEEP_WORDS:
                continue
            if is_titlecase(tok):
                continue
            if tl[0] in ("l", "r"):
                continue  # ignore word-initial l/r

            # If the current token is already a known word, don't second-guess it.
            if tl in lexicon or tl in allow_targets:
                continue

            for i in internal_second_consonant_positions(tok):
                if tl[i] not in ("l", "r"):
                    continue
                cand = swap_lr_at(tok, i)
                if cand in lexicon or cand in allow_targets:
                    issues.append(
                        {
                            "file": filename,
                            "line_no": line_no,
                            "word": tok,
                            "suggestion": cand,
                            "reason": "internal second-consonant (V C L/R V)",
                            "context": line.strip(),
                        }
                    )

    return issues


def main() -> None:
    # ——— CLI ———
    parser = argparse.ArgumentParser(
        description="Flag likely L↔R confusions where L/R is the SECOND consonant of a syllable (internal only)."
    )
    parser.add_argument("path", nargs="?", default=".", help="Path to project folder (default: current directory)")
    parser.add_argument("--chapters-dir", default="chapters", help="Folder with chunked .txt files (default: chapters)")
    parser.add_argument("--reports-dir", default="reports", help="Folder for reports (default: reports)")
    parser.add_argument("--allow-file", help="Optional: extra allowed target words (one per line)")
    args = parser.parse_args()

    target_path = Path(args.path)
    chunk_dir = target_path / args.chapters_dir
    output_path = target_path / args.reports_dir / "r_04_lr_confusion.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Dictionaries
    book_dict = target_path / "dict_book.txt"
    system_words = Path("/usr/share/dict/words")  # optional

    if "dict_custom_txt" not in PATHS:
        print("❌ Missing required config key: paths.dict_custom_txt")
        print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
        sys.exit(1)

    global_dict = REPO_ROOT / PATHS["dict_custom_txt"]

    # Fail fast on required dictionaries
    if not book_dict.exists():
        print(f"❌ Per-book dictionary not found: {book_dict.resolve()}")
        print("Expected a dict_book.txt in the selected book folder.")
        sys.exit(1)

    if not global_dict.exists():
        print(f"❌ Global dictionary not found: {global_dict.resolve()}")
        print("Check paths.dict_custom_txt in etna/scripts/common/config.yaml.")
        sys.exit(1)

    # Build lexicon
    book_words, book_counts = load_dictionary_with_counts(book_dict)
    global_words, global_counts = load_dictionary_with_counts(global_dict)
    lexicon = set(book_words) | set(global_words)

    system_counts: dict[Path, int] = {}
    if system_words.exists():
        sys_words, system_counts = load_dictionary_with_counts(system_words)
        lexicon |= sys_words

    allow_targets = load_allow_targets(Path(args.allow_file)) if args.allow_file else set()

    print("\n📘 Dictionary entries loaded (per file):")
    for p, n in sorted(book_counts.items(), key=lambda kv: kv[0].name.lower()):
        print(f"  - {p.name}: {n:,}")
    for p, n in sorted(global_counts.items(), key=lambda kv: kv[0].name.lower()):
        if p in book_counts:
            continue
        print(f"  - {p.name}: {n:,}")
    for p, n in sorted(system_counts.items(), key=lambda kv: kv[0].name.lower()):
        if p in book_counts or p in global_counts:
            continue
        print(f"  - {p.name}: {n:,}")
    print(f"📦 Total unique lexicon entries: {len(lexicon):,}\n")

    if not chunk_dir.exists():
        print(f"❌ No '{args.chapters_dir}' directory at {chunk_dir}. Run your chunking step first.")
        sys.exit(1)

    all_issues: list[dict] = []
    for p in sorted(chunk_dir.glob("*.txt")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        all_issues.extend(run_checks(text, p.name, lexicon, allow_targets))

    grouped: dict[str, list[dict]] = defaultdict(list)
    for it in all_issues:
        grouped[it["word"].lower()].append(it)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(output_path, "w", encoding="utf-8") as out:
        out.write("# L↔R Confusion Check — Internal Onsets Only (Grouped by Word)\n\n")
        out.write(f"_Generated: {now}_\n\n")
        out.write(f"- Unique words flagged: **{len(grouped)}**\n")
        out.write(f"- Total occurrences: **{len(all_issues)}**\n\n")

        if not all_issues:
            out.write("> No likely internal L↔R confusions found.\n")
        else:
            for word in sorted(grouped.keys()):
                entries = sorted(grouped[word], key=lambda x: (x["file"], x["line_no"]))
                out.write(f"\n---\n\n## {word} — {len(entries)} occurrence(s)\n\n")
                out.write("| File | Line | → Suggestion | Why |\n|---|---:|---|---|\n")
                for it in entries:
                    out.write(
                        f"| {it['file']} | {it['line_no']} | `{it['suggestion']}` | {it['reason']} |\n"
                    )

                out.write("\n<details><summary>Contexts</summary>\n\n")
                for it in entries:
                    out.write(f"- {it['file']} L{it['line_no']}: {it['context']}\n")
                out.write("\n</details>\n")

    print(f"✅ LR check complete — {len(all_issues)} occurrences across {len(grouped)} words")
    print(f"📄 Report written to {output_path}")


if __name__ == "__main__":
    main()
