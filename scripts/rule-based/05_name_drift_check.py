#!/usr/bin/env python3
"""
## Name / Proper Noun Consistency Check — what this script does

This script looks for likely misspellings or drift in **names and place words**
across the book. It focuses on capitalised words that Hunspell doesn’t recognise,
filters out project-specific dictionary entries, then uses fuzzy matching to find
near-duplicates where one spelling is common and another is a rare variant.

Example targets:
- `Treggan` vs `Treggen`
- `Maurice` vs `Muarice`
- `Issey` vs `Issy` (if you’ve only meant one form)

Only pairs with a strong frequency imbalance and high similarity are reported, so
the noise level stays low.
"""

import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict
import re
import subprocess
import sys

from rapidfuzz import fuzz
from tqdm import tqdm

# ——— Config ———

BOOK_DICT = "dict_book.txt"            # per-book (mandatory)
SYSTEM_WORDS = "/usr/share/dict/words"  # optional

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
    """Load a newline-delimited dictionary with optional `#include` support, plus per-file counts.

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


# ——— CLI ———

parser = argparse.ArgumentParser(
    description="Detect likely misspellings or drift in proper names / places."
)
parser.add_argument("path", nargs="?", default=".", help="Base project folder")
parser.add_argument(
    "--chapters-dir", default="chapters", help="Folder with chunked .txt files"
)
parser.add_argument(
    "--reports-dir", default="reports", help="Folder for markdown reports"
)
parser.add_argument(
    "--hunspell-dict",
    default="en_GB",
    help="Hunspell dictionary code (default: en_GB)",
)
args = parser.parse_args()

base_path = Path(args.path)
chapters_dir = base_path / args.chapters_dir
reports_dir = base_path / args.reports_dir
report_path = reports_dir / "r_05_name_drift_check.md"
reports_dir.mkdir(parents=True, exist_ok=True)


# ——— Helpers ———

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def collect_capitalised_tokens():
    """
    Walk chapters and collect all capitalised tokens with their locations.
    Returns:
        occurrences: list of dicts {token, token_lower, file, line_no, context}
        counts: Counter[token_lower] -> total uses
    """
    occurrences = []
    counts = Counter()

    if not chapters_dir.exists():
        print(f"❌ No '{args.chapters_dir}' directory at {chapters_dir}")
        return occurrences, counts

    for path in tqdm(sorted(chapters_dir.glob("*.txt")), desc="Scanning chapters"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        for line_no, line in enumerate(lines, start=1):
            for m in TOKEN_RE.finditer(line):
                tok = m.group(0)
                # capitalised token (first letter uppercase, rest arbitrary)
                if not tok[0].isupper():
                    continue
                if len(tok) < MIN_TOKEN_LENGTH:
                    continue

                tl = tok.lower()
                occurrences.append(
                    {
                        "token": tok,
                        "token_lower": tl,
                        "file": path.name,
                        "line_no": line_no,
                        "context": line.strip(),
                    }
                )
                counts[tl] += 1

    return occurrences, counts


def hunspell_unknown_words(tokens: set[str]) -> set[str]:
    """
    Ask Hunspell which of these tokens it considers *unknown*.
    Returns a set of lowercase tokens that Hunspell doesn't recognise.
    """
    if not tokens:
        return set()

    # Hunspell expects one word per line
    input_text = "\n".join(sorted(tokens)) + "\n"

    cmd = ["hunspell", "-d", args.hunspell_dict, "-l"]
    try:
        proc = subprocess.run(
            cmd,
            input=input_text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        print("❌ hunspell not found on PATH. Install via Homebrew: brew install hunspell")
        return set()

    unknown = set()
    for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
        w = line.strip().lower()
        if w:
            unknown.add(w)
    return unknown


# ——— Main Logic ———

MIN_TOKEN_LENGTH = 3          # shortest proper-noun token we care about
MIN_FUZZY_SIMILARITY = 90     # how similar two name-forms must be
MIN_CANON_FREQ = 3            # minimum occurrences of the canonical form
MAX_VARIANT_FREQ = 2          # maximum occurrences of a suspected variant

# Load local lexicon (words we consider fine even if Hunspell doesn’t know them)
book_dict_path = base_path / BOOK_DICT
if not book_dict_path.exists():
    print(f"❌ Per-book dictionary not found: {book_dict_path.resolve()}")
    print("Expected a dict_book.txt in the selected book folder.")
    sys.exit(1)

if "dict_custom_txt" not in PATHS:
    print("❌ Missing required config key: paths.dict_custom_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

global_dict_path = REPO_ROOT / PATHS["dict_custom_txt"]
if not global_dict_path.exists():
    print(f"❌ Global dictionary not found: {global_dict_path.resolve()}")
    print("Check paths.dict_custom_txt in etna/scripts/common/config.yaml.")
    sys.exit(1)

book_dict, book_counts = load_dictionary_with_counts(book_dict_path)
global_dict, global_counts = load_dictionary_with_counts(global_dict_path)

system_counts: dict[Path, int] = {}
system_words: set[str] = set()
if Path(SYSTEM_WORDS).exists():
    system_words, system_counts = load_dictionary_with_counts(Path(SYSTEM_WORDS))

LOCAL_LEXICON = book_dict | global_dict | system_words

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
print(f"📦 Total unique lexicon entries (after merge): {len(LOCAL_LEXICON):,}\n")

occurrences, counts = collect_capitalised_tokens()
if not occurrences:
    print("✅ No capitalised tokens found.")
    raise SystemExit(0)

all_lower_tokens = {entry["token_lower"] for entry in occurrences}
unknown_by_hunspell = hunspell_unknown_words(all_lower_tokens)

# Candidates: unknown to Hunspell AND not in our local lexicon
candidate_tokens = {
    t
    for t in unknown_by_hunspell
    if t not in LOCAL_LEXICON and len(t) >= MIN_TOKEN_LENGTH
}

print(f"🔍 Candidate unknown proper nouns: {len(candidate_tokens)}")

# Build reverse index: token_lower -> list of occurrences
by_token = defaultdict(list)
for entry in occurrences:
    tl = entry["token_lower"]
    if tl in candidate_tokens:
        by_token[tl].append(entry)

# For clustering, work with unique candidate forms
candidate_list = sorted(candidate_tokens)

# Find suspiciously similar pairs
pairs = []  # (canonical_lower, variant_lower, canon_count, variant_count, score)
seen_pairs = set()

for i in range(len(candidate_list)):
    t1 = candidate_list[i]
    for j in range(i + 1, len(candidate_list)):
        t2 = candidate_list[j]
        # Cheap pre-filter: first letter must match
        if t1[0] != t2[0]:
            continue

        score = fuzz.ratio(t1, t2)
        if score < MIN_FUZZY_SIMILARITY:
            continue

        c1 = counts.get(t1, 0)
        c2 = counts.get(t2, 0)
        hi = max(c1, c2)
        lo = min(c1, c2)

        if hi < MIN_CANON_FREQ or lo == 0 or lo > MAX_VARIANT_FREQ:
            continue

        # canonical = more frequent
        canon = t1 if c1 >= c2 else t2
        var = t2 if canon == t1 else t1
        key = (canon, var)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        pairs.append((canon, var, counts[canon], counts[var], score))

# Group by canonical form
grouped = defaultdict(list)
for canon, var, c_canon, c_var, score in pairs:
    grouped[canon].append((var, c_canon, c_var, score))

# ——— Write report ———

timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

with report_path.open("w", encoding="utf-8") as out:
    out.write("# 🧾 Name / Proper Noun Consistency Report\n\n")
    out.write(f"_Generated: {timestamp}_\n\n")
    out.write(
        f"- Hunspell dictionary: `{args.hunspell_dict}`\n"
        f"- Minimum canonical frequency: `{MIN_CANON_FREQ}`\n"
        f"- Maximum variant frequency: `{MAX_VARIANT_FREQ}`\n"
        f"- Minimum fuzzy similarity: `{MIN_FUZZY_SIMILARITY}`\n\n"
    )

    out.write("## Dictionaries loaded\n\n")
    out.write(f"- Per-book: `{book_dict_path.resolve()}`\n")
    for p, n in sorted(book_counts.items(), key=lambda kv: kv[0].name.lower()):
        out.write(f"  - {p.name}: {n:,}\n")

    out.write(f"- Global: `{global_dict_path.resolve()}`\n")
    for p, n in sorted(global_counts.items(), key=lambda kv: kv[0].name.lower()):
        if p in book_counts:
            continue
        out.write(f"  - {p.name}: {n:,}\n")

    if system_counts:
        out.write(f"- System words: `{Path(SYSTEM_WORDS).resolve()}`\n")
        for p, n in sorted(system_counts.items(), key=lambda kv: kv[0].name.lower()):
            if p in book_counts or p in global_counts:
                continue
            out.write(f"  - {p.name}: {n:,}\n")

    out.write("\n")

    if not grouped:
        out.write("✅ No suspicious name or place drift detected.\n")
    else:
        out.write(
            "This report shows candidate name/place spellings where one form is\n"
            "common in the manuscript and another, very similar form appears only\n"
            "once or twice.\n\n"
        )

        for canon in sorted(grouped.keys()):
            variants = grouped[canon]
            total_canon = counts[canon]
            out.write(f"---\n\n## `{canon}` — {total_canon} occurrence(s)\n\n")
            out.write("| Variant | Canon count | Variant count | Fuzzy score |\n")
            out.write("|---------|-------------|---------------|-------------|\n")
            for var, c_canon, c_var, score in sorted(
                variants, key=lambda x: (-x[2], -x[3])
            ):
                out.write(f"| `{var}` | {c_canon} | {c_var} | {score:.1f} |\n")
            out.write("\n")

            # Show contexts for canonical + variants
            out.write("### Contexts\n\n")
            out.write(f"**Canonical `{canon}`:**\n\n")
            for occ in by_token[canon][:10]:  # limit for sanity
                out.write(f"- {occ['file']} L{occ['line_no']}: {occ['context']}\n")
            out.write("\n")

            for var, _, _, _ in variants:
                out.write(f"**Variant `{var}`:**\n\n")
                for occ in by_token[var]:
                    out.write(f"- {occ['file']} L{occ['line_no']}: {occ['context']}\n")
                out.write("\n")

# Terminal summary: path first, then a concise final status line.
num_groups = len(grouped)
num_pairs = sum(len(v) for v in grouped.values())

print(f"Report written to {report_path}")

if not grouped:
    print("✅ No suspicious name/place drift detected")
else:
    print(f"⚠️  Possible name/place drift: {num_groups} name(s), {num_pairs} variant pair(s)")