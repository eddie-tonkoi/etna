#!/usr/bin/env python3
"""
## Duplicate Phrase Check — what this script does

This tool hunts for **near-identical lines** across your chapter files — the
sort of repeated phrasing that often comes from copying a line, rewriting it in
another chapter, but not quite matching the original wording. It’s especially
useful for catching stock phrases that have drifted.

It works in several stages:

1. **Extracts meaningful lines from each chapter**
   - Loads each `.txt` file in `chapters/`.
   - Strips chapter headings.
   - Ignores very short lines and common structural headings such as
     *Tuesday afternoon*, *Sunday evening*, or all‑caps register labels.
   - Each remaining line (with its filename and line number) becomes a candidate
     phrase.

2. **Filters with a token-overlap test**
   - Before doing any fuzzy matching, the script checks whether two lines share
     enough meaningful words.
   - This removes most of the noise and keeps the comparisons fast.

3. **Uses a very high fuzzy threshold**
   - Only lines with a fuzz ratio ≥ `95` are kept — these are truly
     near-identical.
   - Whitelisted phrases are skipped entirely.

4. **Counts how often each phrase appears**
   - Builds a global frequency map so the report can highlight when one phrase
     is a “standard form” (used many times) and another is a one‑off variant.

5. **Sorts by significance**
   - Results are ordered by:
     - highest occurrence count,
     - then asymmetry between the two phrases,
     - then fuzzy score.
   - This floats genuine drifts to the top.

6. **Writes a clear markdown report**
   - For each match, shows:
     - the two locations,
     - the fuzzy score,
     - how often each phrase appears,
     - and the two phrase blocks.
   - If one phrase is widely used and the other appears only once, the report
     adds a warning line so you can spot a likely unintended change.

The goal is to be quiet but sharp — cutting out structural noise and surfacing
only the lines that are suspiciously similar, where a repeated phrase may not
have stayed consistent.
"""
import argparse
from pathlib import Path
from datetime import datetime
from rapidfuzz import fuzz
from tqdm import tqdm
import re
import sys

# ——— Settings ———
FUZZY_THRESHOLD = 95            # Very high, only near-identical lines
MIN_PHRASE_LENGTH = 15         # Skip very short lines/headings
TOKEN_OVERLAP_THRESHOLD = 0.6  # Strong token overlap required
MAX_RESULTS = 500

# ——— CLI Setup ———
parser = argparse.ArgumentParser(description="Detect duplicate lines or phrases across chapters.")
parser.add_argument("path", nargs="?", default=".", help="Base project folder")
parser.add_argument(
    "--whitelist-file",
    default=None,
    help=(
        "Whitelist file to skip known repeated phrases. If omitted, uses paths.duplicate_whitelist_txt "
        "from scripts/common/config.yaml. You may pass an absolute path, or a path relative to the project folder "
        "or repo root."
    ),
)
args = parser.parse_args()

base_path = Path(args.path)

# ---------------- Config (mandatory for house_rules paths) ----------------
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

if "duplicate_whitelist_txt" not in PATHS:
    print("❌ Missing required config key: paths.duplicate_whitelist_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

DEFAULT_WHITELIST_PATH = REPO_ROOT / PATHS["duplicate_whitelist_txt"]

chunk_dir = base_path / "chapters"
if not chunk_dir.exists():
    print(f"❌ No 'chapters' directory at {chunk_dir}. Run your chunking step first.")
    sys.exit(1)
report_path = base_path / "reports" / "r_09_duplicate_phrase_check.md"
report_path.parent.mkdir(parents=True, exist_ok=True)

# Common day/time headings like "Tuesday afternoon", "Sunday evening", etc.
DAY_TIME_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"\s+(morning|afternoon|evening|night|at noon)\.?$",
    re.IGNORECASE,
)


# ——— Load whitelist ———
def load_whitelist(path: Path) -> set[str]:
    if not path.exists():
        print(f"❌ Whitelist file not found: {path.resolve()}")
        if args.whitelist_file:
            print("Checked project-relative and repo-relative locations.")
        else:
            print("Check paths.duplicate_whitelist_txt in etna/scripts/common/config.yaml.")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

# Resolve whitelist file:
# - If --whitelist-file is provided, accept absolute or relative paths.
#   For relative paths, try relative-to-project first, then relative-to-repo.
# - If omitted, use the config default (repo-relative).
if args.whitelist_file:
    candidate = Path(args.whitelist_file)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates = [base_path / candidate, REPO_ROOT / candidate]

    whitelist_path = next((p for p in candidates if p.exists()), candidates[0])
else:
    whitelist_path = DEFAULT_WHITELIST_PATH

whitelist = load_whitelist(whitelist_path)
print(f"📘 Loaded {len(whitelist)} whitelist phrases from {whitelist_path}")

# ——— Extract text lines from files ———
def extract_phrases(file_path):
    text = file_path.read_text(encoding="utf-8")
    lines = [l.strip() for l in text.splitlines()]
    
    # Skip first line if it's a chapter heading
    if lines and re.match(r"^chapter \d+(\.|:.*)?$", lines[0], re.IGNORECASE):
        lines = lines[1:]

    filtered = []
    for idx, line in enumerate(lines, start=1):
        if len(line) < MIN_PHRASE_LENGTH:
            continue

        # Skip day/time headings like "Tuesday afternoon", "Sunday evening"
        if DAY_TIME_RE.match(line):
            continue

        # Skip all‑caps labels (e.g. register headings)
        # If a line is all uppercase (ignoring non-letters), treat it as a label.
        letters_only = re.sub(r"[^A-Za-z]+", "", line)
        if letters_only and letters_only.upper() == letters_only:
            continue

        filtered.append((file_path.name, idx, line))

    return filtered

# ——— Token overlap heuristic ———
def has_token_overlap(a, b, threshold=TOKEN_OVERLAP_THRESHOLD):
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold

# ——— Build phrase list ———
all_phrases = []
for file in sorted(chunk_dir.glob("*.txt")):
    all_phrases.extend(extract_phrases(file))

from collections import Counter
PHRASE_COUNTS = Counter(p for (_, _, p) in all_phrases)

print(f"🔍 Checking fuzzy matches across {len(all_phrases)} phrases...")

# ——— Compare all unique pairs ———
seen = set()
matches = []

for i in tqdm(range(len(all_phrases)), desc="Fuzzy compare"):
    file1, line1, phrase1 = all_phrases[i]
    for j in range(i + 1, len(all_phrases)):
        file2, line2, phrase2 = all_phrases[j]

        if (file1 == file2 and line1 == line2) or phrase1 == phrase2:
            continue
        if phrase1 in whitelist or phrase2 in whitelist:
            continue

        if not has_token_overlap(phrase1, phrase2):
            continue

        score = fuzz.token_sort_ratio(phrase1, phrase2)
        if score >= FUZZY_THRESHOLD:
            key = tuple(sorted([phrase1, phrase2]))
            if key not in seen:
                seen.add(key)
                matches.append((score, file1, line1, phrase1, file2, line2, phrase2))

# ——— Sort and output ———
def match_sort_key(item):
    score, file1, line1, p1, file2, line2, p2 = item
    c1 = PHRASE_COUNTS.get(p1, 1)
    c2 = PHRASE_COUNTS.get(p2, 1)
    hi = max(c1, c2)
    diff = abs(c1 - c2)
    # Sort primarily by highest occurrence count, then by asymmetry, then by score
    return (hi, diff, score)

matches.sort(key=match_sort_key, reverse=True)

with open(report_path, "w", encoding="utf-8") as f:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f.write("# 🔁 Duplicate Phrase Report\n")
    f.write(f"_Generated: {timestamp}_\n\n")
    f.write(f"**Thresholds:** Fuzzy ≥ `{FUZZY_THRESHOLD}`, Min Length ≥ `{MIN_PHRASE_LENGTH}`, Max Results = `{MAX_RESULTS}`\n\n")

    for score, file1, line1, p1, file2, line2, p2 in matches[:MAX_RESULTS]:
        f.write(f"## 🔍 `{file1}:{line1}` vs `{file2}:{line2}`\n")
        f.write(f"**Score:** `{score:.1f}`\n\n")
        count1 = PHRASE_COUNTS.get(p1, 1)
        count2 = PHRASE_COUNTS.get(p2, 1)
        f.write(f"**Occurrences:** Phrase 1 = {count1}, Phrase 2 = {count2}\n")

        # Highlight asymmetric usage: one phrase is a stock line, the other a one-off
        hi = max(count1, count2)
        lo = min(count1, count2)
        if hi >= 5 and lo == 1:
            f.write(
                "\n> ⚠️ Asymmetric usage: one phrase is a repeated line "
                f"({hi} uses) while the other appears only once. "
                "Check for an unintended drift from the standard wording.\n"
            )

        f.write("\n\n**Phrase 1:**\n")
        f.write("```text\n")
        f.write(p1.strip()[:200] + "\n")
        f.write("```\n\n")
        f.write("**Phrase 2:**\n")
        f.write("```text\n")
        f.write(p2.strip()[:200] + "\n")
        f.write("```\n\n")


print(f"✅ Duplicate check complete — {len(matches)} matches found")
print(f"📄 Report saved to {report_path}")
