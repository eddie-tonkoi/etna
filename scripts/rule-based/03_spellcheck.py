#!/usr/bin/env python3
"""
## Spelling Check — what this script does

This script takes the chunked `.txt` files from the `chapters/` folder and runs
a spelling pass over them using Hunspell, plus a couple of house-style layers on
top.

It does three main things:

1. **Loads your custom dictionaries and house rules**
   - Pulls in `dict_book.txt` for project-specific words (names, places, jargon).
   - Splits that into:
     - multi-word phrases (which are later masked out),
     - exact-case words,
     - and lowercase forms.
   - Loads an optional `dict_flag_words.txt` mapping “wrong ⇒ right” for words you
     always want to fix (even if Hunspell would accept them).
   - You can suppress a specific flagged word by adding its UPPERCASE form (e.g. TRUCK) to dict_book.txt.

2. **Asks Hunspell what it thinks, then filters the noise**
   - Normalises smart quotes to plain quotes and strips out bracketed comments
     before checking.
   - Masks any custom phrases so Hunspell doesn’t try to “correct” them.
   - Feeds all candidate tokens to `hunspell -l` to find unknown words.
   - Filters those unknowns against your custom dictionary so known, approved
     words aren’t flagged.
   - For the remaining suspects, calls `hunspell -a` to get suggested corrections.
   - Builds a list of issues:
     - type: `flagged` (from your house rules) or `spelling` (from Hunspell),
     - the word itself,
     - a short context snippet,
     - and the top suggestion, if there is one.

3. **Writes a markdown report you can actually work through**
   - `r_03_spellcheck.md` starts with a header: where it ran, how many custom
     dictionary entries and flagged words it loaded, and how many issues it found.
   - Shows a quick “issues by file” breakdown so you can see which chapters are
     noisiest.
   - Groups everything by word:
     - shows the issue types involved,
     - the suggested correction (if any),
     - how many times it appears,
     - and a bullet list of contexts with the chunk filename.

It’s not trying to auto-fix anything. Its only job is to surface likely spelling
problems and house-rule violations in one place, so you can make deliberate
choices as you edit.
"""

import argparse
from pathlib import Path
from datetime import datetime
import subprocess
import re
import sys
from collections import Counter, defaultdict

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


def load_dictionary_with_includes(path: Path, _seen: set[Path] | None = None) -> set[str]:
    """Load a newline-delimited dictionary with optional `#include` support.

    Rules:
    - Blank lines are ignored.
    - Lines starting with `#` are comments, except `#include <path>`.
    - `#include` paths may be absolute or relative to the including file.
    - Includes are recursive with cycle protection.

    Note: The global dictionary is loaded explicitly via config.yaml, but `#include`
    is still supported so per-book dictionaries can pull in series-wide wordlists.
    """
    if _seen is None:
        _seen = set()

    resolved = path.expanduser().resolve()
    if resolved in _seen:
        return set()
    _seen.add(resolved)

    if not resolved.exists():
        print(f"❌ Dictionary file not found: {resolved}")
        sys.exit(1)

    words: set[str] = set()

    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("#include"):
                # Allow: #include path/to/file.txt
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

                words |= load_dictionary_with_includes(include_resolved, _seen=_seen)
                continue

            if line.startswith("#"):
                continue

            words.add(line)

    return words


# ---- Added: loader with per-file word counts ----
def load_dictionary_with_counts(path: Path, _seen: set[Path] | None = None, _counts: dict[Path, int] | None = None) -> tuple[set[str], dict[Path, int]]:
    """Like `load_dictionary_with_includes`, but also returns a {file_path: local_unique_count} map.

    The count for each file is the number of unique, non-comment, non-include entries present in that file itself.
    Included files are counted separately and merged recursively.
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

    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
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

                included_words, _counts = load_dictionary_with_counts(include_resolved, _seen=_seen, _counts=_counts)
                all_words |= included_words
                continue

            if line.startswith("#"):
                continue

            local_words.add(line)

    _counts[resolved] = len(local_words)
    all_words |= local_words

    return all_words, _counts


# ---------------- CLI ----------------
parser = argparse.ArgumentParser(description="Fix spelling in pre-chunked text files.")
parser.add_argument("path", nargs="?", default=".", help="Path to folder (default: current directory)")
parser.add_argument("--lang", default="en_GB", help="Hunspell dictionary language (default: en_GB)")
parser.add_argument("--debug", action="store_true", help="Enable debug prints and show hunspell stderr")
args = parser.parse_args()

target_path = Path(args.path)
print(f"🔍 Running spelling fix on: {target_path.resolve()}")

DICT_BOOK = target_path / "dict_book.txt"

REPORT = target_path / "reports/r_03_spellcheck.md"

# UPPERCASE tokens in dict_book.txt matching this pattern are treated as
# per-book allow-list entries for house-rule flagged words.
# (Avoids accidentally treating section headers like ###_NAMES_### as allow rules.)
ALLOW_FLAGGED_TOKEN_RE = re.compile(r"^[A-Z][A-Z'-]*$")


# ---------------- Helpers ----------------
def load_flag_words(path: Path):
    """Load flagged words mapping 'wrong => right' (case-insensitive keys)."""
    flag_map = {}
    if not path.exists():
        print(f"❌ Flag words file not found: {path.resolve()}")
        print("Check paths.dict_flag_words_txt in etna/scripts/common/config.yaml.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" in line:
                wrong, right = map(str.strip, line.split("=>", 1))
                if wrong:
                    flag_map[wrong.lower()] = right
    print(f"✅ Loaded {len(flag_map):,} flagged word{'s' if len(flag_map) != 1 else ''} from: {path.resolve()}")
    return flag_map


def categorize_dictionary(words):
    """Split custom words into multi-word phrases, exact-case words, and lowercase words."""
    phrases, exact, lower = set(), set(), set()
    for word in words:
        if not word:
            continue
        # Only treat real multi-word entries as phrases; keep hyphenated words spell-checkable.
        if " " in word or "…" in word:
            phrases.add(word)
        elif word.islower():
            lower.add(word)
        else:
            exact.add(word)
    return phrases, exact, lower


def find_word_context(original_text, word):
    target = re.sub(r"[^\w']", "", word).lower()

    # Normalize the original text
    normalized_text = (
        original_text.replace("’", "'")
        .replace("—", " ")
        .replace("…", " ")
    )

    # Split to sentence-ish chunks
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', normalized_text)

    # Try to find the sentence containing the target word
    for sent in sentences:
        normalized = [re.sub(r"[^\w']", "", w).lower() for w in sent.split()]
        if target in normalized:
            return sent.strip()

    # Try fuzzy matching for clipped/fractured words (apol…, ‘ru…mee…’)
    fuzzy = re.findall(rf"\b\w*{re.escape(target)}\w*\b", normalized_text.lower())
    if fuzzy:
        idx = normalized_text.lower().find(fuzzy[0])
        return normalized_text[max(0, idx - 60): idx + 60].strip()

    # Final fallback: first 120 chars
    return original_text.strip()[:120]


def run_hunspell_list(words, lang, debug=False):
    """Return set of unknown words from hunspell -l.

    This will exit with an error message if Hunspell is not installed or
    if the underlying process fails (non-zero exit code).
    """
    if not words:
        return set()

    try:
        p = subprocess.Popen(
            ["hunspell", "-l", "-d", lang, "-i", "UTF-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("❌ Error: 'hunspell' command not found. Please install Hunspell and ensure it is on your PATH.")
        sys.exit(1)

    feed = "\n".join(words) + "\n"
    out, err = p.communicate(feed)

    if debug:
        print(f"[debug] hunspell -l exit={p.returncode}")
        if err:
            print("[debug] hunspell stderr:")
            print(err.strip())

    if p.returncode != 0:
        message = (err or "").strip() or "Unknown hunspell error"
        print(f"❌ Hunspell -l failed (exit {p.returncode}).")
        if message:
            print(message)
        sys.exit(1)

    return {w.strip() for w in out.splitlines() if w.strip()}


def run_hunspell_analyze(words, lang, debug=False):
    """Return map {word: [suggestions]} using hunspell -a.

    This will exit with an error message if Hunspell is not installed or
    if the underlying process fails (non-zero exit code).
    """
    if not words:
        return {}

    feed_list = list(words)
    feed = "\n".join(feed_list) + "\n"

    try:
        p = subprocess.Popen(
            ["hunspell", "-a", "-d", lang, "-i", "UTF-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("❌ Error: 'hunspell' command not found. Please install Hunspell and ensure it is on your PATH.")
        sys.exit(1)

    out, err = p.communicate(feed)

    if debug:
        print(f"[debug] hunspell -a exit={p.returncode}")
        if err:
            print("[debug] hunspell stderr:")
            print(err.strip())

    if p.returncode != 0:
        message = (err or "").strip() or "Unknown hunspell error"
        print(f"❌ Hunspell -a failed (exit {p.returncode}).")
        if message:
            print(message)
        sys.exit(1)

    suggestion_map = {}
    # First line is usually "Hunspell x.y.z"
    lines = out.splitlines()[1:]
    idx = 0
    for line in lines:
        if not line:
            continue
        # "*"/"#": correct or unknown without suggestions
        if line.startswith(("*", "#")):
            if idx < len(feed_list):
                suggestion_map[feed_list[idx]] = []
                idx += 1
        elif line.startswith("&"):
            # & misspelled N X: suggestions...
            parts = line.split(":", 1)
            suggs = parts[1].strip().split(", ") if len(parts) > 1 else []
            if idx < len(feed_list):
                suggestion_map[feed_list[idx]] = suggs
                idx += 1
        else:
            # Some other line; advance cautiously
            if idx < len(feed_list) and feed_list[idx] not in suggestion_map:
                suggestion_map[feed_list[idx]] = []
                idx += 1

    # Ensure all inputs have an entry
    for j in range(idx, len(feed_list)):
        suggestion_map.setdefault(feed_list[j], [])
    return suggestion_map


def check_spelling(chunk, custom_words, flag_words=None, lang="en_GB", debug=False):
    original_chunk = chunk  # preserve original casing for context
    chunk = chunk.replace("’", "'")  # normalize smart quotes only
    # Mask bracketed comments
    chunk = re.sub(r"\[.*?\]", "", chunk, flags=re.DOTALL)

    # Categorize dictionary
    phrases, custom_exact, custom_lower = categorize_dictionary(custom_words)

    # Allow-list for house-rule "flagged" words.
    # Convention: put the UPPERCASE form of a flagged word (e.g. `TRUCK`) in dict_book.txt
    # to suppress that specific flagged warning for this project.
    allowed_flagged = {w.lower() for w in custom_exact if ALLOW_FLAGGED_TOKEN_RE.fullmatch(w)}

    if debug:
        # Keep this lightweight: show whether the common case is enabled and how many allow-rules exist.
        print(f"[debug] allowed_flagged_count={len(allowed_flagged)}")
        if "truck" in allowed_flagged:
            print("[debug] allow rule enabled: TRUCK")

    # Mask phrases from dictionary (longest first)
    PHRASE_MARKER = "CUSTOMPHRASE"
    masked_chunk = chunk
    for i, phrase in enumerate(sorted(phrases, key=len, reverse=True)):
        token = f"{PHRASE_MARKER}{i}"
        pattern = re.escape(phrase)
        masked_chunk = re.sub(pattern, token, masked_chunk)

    errs = []

    # Flagged words pass (case-insensitive check over original/normalized tokens)
    if flag_words:
        for w in re.findall(r"\b[\w'-]+\b", chunk):
            lw = w.lower()
            if lw in flag_words:
                # If explicitly allow-listed (e.g. `TRUCK` in dict_book.txt), suppress.
                if lw in allowed_flagged:
                    if debug:
                        print(f"[debug] suppressed flagged rule {lw.upper()} for token={w!r}")
                    continue
                ctx = find_word_context(original_chunk, w)
                errs.append({
                    "type": "flagged",
                    "rule": lw.upper(),
                    "word": w,
                    "context": ctx,
                    "suggestion": flag_words[lw],
                })

    # Tokenize
    tokens = re.findall(r"\b[\w'-]+\b", masked_chunk)

    # Build candidate list (let Hunspell decide; filter custom AFTER)
    candidates = []
    for w in tokens:
        if w.startswith(PHRASE_MARKER) or re.fullmatch(r"[0-9.,%]+", w) or w.isupper():
            continue
        candidates.append(w)

    if debug:
        print(f"[debug] token_count={len(tokens)} candidates={len(candidates)}")

    if not candidates:
        return errs  # might still have flagged items

    # Ask Hunspell for unknowns
    unknowns = run_hunspell_list(candidates, lang, debug=debug)
    if debug:
        print(f"[debug] unknowns_before_filter={len(unknowns)}")

    if not unknowns:
        return errs

    # Filter unknowns using custom dictionary AFTER hunspell
    filtered_unknowns = set()
    for w in unknowns:
        base = (
            w[:-2] if w.lower().endswith("'s") else
            w[:-1] if w.endswith("s") and len(w) > 3 else
            w
        )
        if (w in custom_exact or w.lower() in custom_lower or
                base in custom_exact or base.lower() in custom_lower):
            continue
        filtered_unknowns.add(w)

    if debug:
        print(f"[debug] unknowns_after_custom_filter={len(filtered_unknowns)}")

    if not filtered_unknowns:
        return errs

    # Get suggestions
    # Use stable order so mapping aligns
    ordered_unknowns = sorted(filtered_unknowns, key=str.lower)
    suggestion_map = run_hunspell_analyze(ordered_unknowns, lang, debug=debug)

    # Build error entries with context
    for w in tokens:
        if w in filtered_unknowns:
            corr = suggestion_map.get(w, [])
            top = corr[0] if corr else ""
            ctx = find_word_context(original_chunk, w)
            errs.append({
                "type": "spelling",
                "word": w,
                "context": ctx,
                "suggestion": top
            })

    # Deduplicate (word + context)
    seen = set()
    unique_errs = []
    for err in errs:
        key = (err["word"].lower(), err["context"])
        if key not in seen:
            seen.add(key)
            unique_errs.append(err)

    return unique_errs


# ---------------- Main ----------------
def main():
    Path(REPORT.parent).mkdir(parents=True, exist_ok=True)
    chunk_dir = target_path / "chapters"
    if not chunk_dir.exists():
        print(f"❌ No chunks found in {chunk_dir}.")
        return

    # Load dictionaries (mandatory)
    if not DICT_BOOK.exists():
        print(f"❌ Per-book dictionary not found: {DICT_BOOK.resolve()}")
        print("Expected a dict_book.txt in the selected book folder.")
        sys.exit(1)

    if "dict_custom_txt" not in PATHS:
        print("❌ Missing required config key: paths.dict_custom_txt")
        print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
        sys.exit(1)

    DICT_GLOBAL = REPO_ROOT / PATHS["dict_custom_txt"]
    if not DICT_GLOBAL.exists():
        print(f"❌ Global dictionary not found: {DICT_GLOBAL.resolve()}")
        print("Check paths.dict_custom_txt in etna/scripts/common/config.yaml.")
        sys.exit(1)

    global_words, global_counts = load_dictionary_with_counts(DICT_GLOBAL)
    book_words, book_counts = load_dictionary_with_counts(DICT_BOOK)
    custom_words = set(global_words) | set(book_words)

    print("\n📘 Dictionary entries loaded (per file):")
    for p, n in sorted(book_counts.items(), key=lambda kv: kv[0].name.lower()):
        print(f"  - {p.name}: {n:,}")
    for p, n in sorted(global_counts.items(), key=lambda kv: kv[0].name.lower()):
        # Avoid double-print if the same file is included from both sides.
        if p in book_counts:
            continue
        print(f"  - {p.name}: {n:,}")
    print(f"📦 Total unique dictionary entries (after merge): {len(custom_words):,}\n")

    if "dict_flag_words_txt" not in PATHS:
        print("❌ Missing required config key: paths.dict_flag_words_txt")
        print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
        sys.exit(1)

    FLAG_WORDS_FILE = REPO_ROOT / PATHS["dict_flag_words_txt"]
    flag_words = load_flag_words(FLAG_WORDS_FILE)

    spelling = []
    chunk_files = sorted(chunk_dir.glob("*.txt"))
    if not chunk_files:
        print(f"❌ No .txt files found in {chunk_dir}")
        return

    for i, f in enumerate(chunk_files, 1):
        chunk = f.read_text(encoding="utf-8", errors="replace")
        print(f"🔍 Spellcheck Chunk {i}/{len(chunk_files)} — {f.name}")
        issues = check_spelling(chunk, custom_words, flag_words=flag_words, lang=args.lang, debug=args.debug)
        print(f"   ↳ {len(issues)} issue(s)")
        for issue in issues:
            issue["chunk"] = f.name
        spelling.extend(issues)

    print(f"🧮 Total issues found: {len(spelling)}")

    # -------- Write report --------
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("# 📝 Spelling Report (Grouped by Word)\n")
        f.write(f"_Generated: {ts}_\n\n")

        # Header info
        f.write("## 🔍 Report Header Information\n")
        f.write(f"🔍 Running spelling fix on: {target_path.resolve()}\n")
        f.write(f"📘 Loaded {len(book_words):,} entries from: {DICT_BOOK.resolve()}\n")
        for p, n in sorted(book_counts.items(), key=lambda kv: kv[0].name.lower()):
            f.write(f"    - {p.name}: {n:,}\n")

        f.write(f"📘 Loaded {len(global_words):,} entries from: {DICT_GLOBAL.resolve()}\n")
        for p, n in sorted(global_counts.items(), key=lambda kv: kv[0].name.lower()):
            if p in book_counts:
                continue
            f.write(f"    - {p.name}: {n:,}\n")
        f.write(f"📦 Total unique dictionary entries loaded: {len(set(custom_words)):,}\n")
        if flag_words:
            f.write(f"✅ Loaded {len(flag_words):,} flagged words from: {FLAG_WORDS_FILE.resolve()}\n")
        f.write(f"🧮 Total issues found: {len(spelling):,}\n\n")

        # Per-file summary
        if spelling:
            by_chunk = Counter(e.get("chunk", "unknown") for e in spelling)
            f.write("## 📄 Issues by file\n")
            for chunk_name, count in sorted(by_chunk.items()):
                f.write(f"- {chunk_name}: {count}\n")
            f.write("\n")

        # Group by word
        if not spelling:
            f.write("## ✅ No issues found.\n")
        else:
            by_word = defaultdict(list)
            for e in spelling:
                by_word[e["word"]].append(e)

            for word in sorted(by_word, key=lambda w: w.lower()):
                items = by_word[word]
                types = {e["type"] for e in items}
                sugg = next((e["suggestion"] for e in items if e.get("suggestion")), "")
                f.write(f"## {word}\n")
                f.write(f"- Types: {', '.join(sorted(types))}\n")
                if sugg:
                    f.write(f"- Suggestion: **{sugg}**\n")
                rules = {e.get("rule") for e in items if e.get("type") == "flagged" and e.get("rule")}
                if rules:
                    f.write(f"- Rule: {', '.join(sorted(rules))} (add to dict_book.txt to allow)\n")
                f.write(f"- Occurrences: {len(items)}\n\n")
                for e in items:
                    chunk = e.get("chunk", "unknown")
                    ctx = (e.get("context") or "").replace("\n", " ").strip()
                    f.write(f"  - `{chunk}` — {ctx}\n")
                f.write("\n")

    print(f"✅ Wrote report to {REPORT}")


if __name__ == "__main__":
    main()
