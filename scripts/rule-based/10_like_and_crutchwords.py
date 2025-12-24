#!/usr/bin/env python3
"""
## Style Expression Check — how this script works

This tool looks at how you use *like* across the whole manuscript, along with a
few common crutch words and any clichés you’ve defined. It’s designed to show
patterns, not prescribe anything — just a clear picture of how often certain
phrases turn up and where.

It does four main things:

1. **Counts key crutch words across the full book**
   - Tracks `like`, `just`, `really`, `very`, `suddenly`.
   - Normalises counts per 10,000 words so chapters of different lengths are
     comparable.
   - Flags gentle warnings when a word sits above its soft guideline.

2. **Finds ‘like [noun]’ similes using spaCy**
   - Parses each chapter, spotting `like` followed by a noun phrase.
   - Cleans and normalises them (e.g., groups variants such as *like the man*,
     *like a man* → *like man*).
   - Records where each simile occurs.
   - Only reports similes that repeat **and** appear in chapters too close
     together — spaced-out imagery is fine; clusters get noted.

3. **Checks for configured cliché phrases**
   - Loads `cliches.txt` if present.
   - Reports each match with context.

4. **Produces two outputs:**
   - A bar chart showing each chapter’s normalised ‘like’ rate.
   - A markdown report summarising:
     - overall counts,
     - per-word crutch usage,
     - repeated simile patterns and their locations,
     - any cliché matches.

The goal isn’t to nag — it’s to give you a clear stylistic snapshot so you can
spot over-familiar images or accidental repetition at a glance.
"""
import argparse
import re
import matplotlib.pyplot as plt
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
import spacy
from tqdm import tqdm
import sys

# --- Style Configuration ---
TARGET_WORDS = {
    # word       # soft guideline per 10k words (tweak to taste)
    "like":      {"per_10k_warning": 80},
    "just":      {"per_10k_warning": 60},
    "really":    {"per_10k_warning": 40},
    "very":      {"per_10k_warning": 30},
    "suddenly":  {"per_10k_warning": 20},
}

CLICHE_CONFIG = {
    "min_hits_to_report": 1,
    "case_insensitive": True,
    "context_chars": 60,
}

SIMILE_CONFIG = {
    # Only report a 'like [noun]' phrase if it repeats at least this many times
    "min_repeats_to_report": 2,
    # If all uses of a phrase are separated by at least this many chapters,
    # it is treated as safely spaced and not reported as "repeated".
    "min_chapter_gap_ok": 8,
    # Only show simile head nouns that reach this count
    "head_min_count": 2,
}

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------- Config (mandatory for house_rules paths) ----------------

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

if "cliches_txt" not in PATHS:
    print("❌ Missing required config key: paths.cliches_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

PHRASE_FILE = REPO_ROOT / PATHS["cliches_txt"]
if not PHRASE_FILE.exists():
    print(f"❌ Cliché list not found: {PHRASE_FILE.resolve()}")
    print("Check paths.cliches_txt in etna/scripts/common/config.yaml.")
    sys.exit(1)

try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    print("❌ spaCy model 'en_core_web_sm' is not installed.")
    print("Install with: python -m spacy download en_core_web_sm")
    sys.exit(1)

# --- Manual Overrides ---
MANUAL_GROUPS = {
    "like the man": "like man",
    "like men": "like man",
    "like a man": "like man",
    "like children": "like child",
    "like a wounded man": "like man",
    "like a million": "like crowd",
    "like a billion": "like crowd",
    # Add more as needed
}

SKIP_PHRASES = {
    # e.g. "like time", "like way"
}

# Reference data block (editable)
reference_examples = [
    ("Raising Steam (Terry Pratchett)", 126097, 453),
    ("Long Call (Cleeves)", 105816, 262),
    ("American Gods (Neil Gaiman)", 217087, 735),
    ("House Cerulean (Klune)", 116621, 392),
    # Add more later
]

ref_section = "### 📘 Reference Benchmarks\n"
ref_section += "_(For comparison — approximate word and 'like' counts)_\n\n"
for title, wc, likes in reference_examples:
    ref_section += f"- **{title}**: {wc:,} words, {likes} 'like' occurrences\n"
ref_section += "\n"

# --- Frequency Counting ---
def count_word_occurrences(text, word):
    """Count whole-word occurrences of a target word, case-insensitive."""
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    return len(pattern.findall(text))

def chunk_text_by_words(text, chunk_size=500):
    words = text.split()
    return [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]

# --- Pattern Matching ---
def extract_like_noun_phrases(text):
    doc = nlp(text)
    results = []
    contexts = []

    for i, token in enumerate(doc):
        if token.text.lower() == "like":
            # Look ahead for noun chunk
            chunk = None
            for np in doc.noun_chunks:
                if np.start == i + 1:
                    chunk = np
                    break

            if chunk:
                head = chunk.root.lemma_.lower()
                phrase = "like " + head
                results.append(phrase)
                contexts.append((phrase, token.sent.text.strip()))
            else:
                # Fallback: scan next tokens
                span = doc[i+1:i+6]
                for j, tok in enumerate(span):
                    if tok.pos_ in {"NOUN", "PROPN", "PRON"}:
                        left = [t.text for t in span[:j] if t.pos_ in {"ADJ", "ADV", "DET"}]
                        phrase = "like " + " ".join(left + [tok.lemma_])
                        results.append(phrase.lower())
                        contexts.append((phrase.lower(), token.sent.text.strip()))
                        break

    # Apply filtering/grouping
    filtered_results = []
    filtered_contexts = []
    for phrase, context in zip(results, contexts):
        normalized = MANUAL_GROUPS.get(phrase, phrase)

        if normalized in SKIP_PHRASES:
            continue

        doc_phrase = nlp(normalized)
        if len(doc_phrase) >= 2:
            token_after_like = doc_phrase[1]
            if token_after_like.pos_ == "PRON":
                continue
            if token_after_like.lemma_.lower() in {"this", "that", "these", "those"}:
                continue

        filtered_results.append(normalized)
        filtered_contexts.append((normalized, context[1]))

    return filtered_results, filtered_contexts

# --- Main ---
def main():
    parser = argparse.ArgumentParser(description="Analyze frequency and patterns of 'like' in text.")
    parser.add_argument("dir", help="Root folder containing 'chapters/'")
    args = parser.parse_args()

    root_dir = Path(args.dir)
    chapter_dir = root_dir / "chapters"
    if not chapter_dir.exists():
        print(f"❌ No 'chapters' directory at {chapter_dir}. Run your chunking step first.")
        sys.exit(1)

    report_dir = root_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    report_path = report_dir / "r_10_like_and_crutchwords.md"
    plot_path = report_dir / "r_10_like_and_crutchwords.png"

    like_phrase_counter = Counter()
    like_phrase_examples = defaultdict(list)  # {phrase: [(chapter_index, filename, snippet)]}

    full_text = ""
    chapter_stats = []  # [(chapter_name, word_count, like_count)]

    # --- Load cliché phrase list (required) ---
    cliche_phrases = [
        line.strip()
        for line in PHRASE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    flags = re.IGNORECASE if CLICHE_CONFIG["case_insensitive"] else 0
    phrase_patterns = [
        (phrase, re.compile(rf"\b{re.escape(phrase)}\b", flags))
        for phrase in cliche_phrases
    ]

    cliche_matches = defaultdict(list)  # phrase → list of (chapter, context)

    chapter_files = sorted(chapter_dir.glob("*.txt"))
    for chapter_index, file in enumerate(tqdm(chapter_files, desc="Processing chapters"), start=1):
        text = file.read_text(encoding="utf-8")
        full_text += text + "\n"  # accumulate text from each chapter

        # Per-chapter statistics for normalised 'like' rate
        chapter_word_count = len(text.split())
        chapter_like_count = count_word_occurrences(text, "like")
        chapter_stats.append((file.name, chapter_word_count, chapter_like_count))

        phrases, contexts = extract_like_noun_phrases(text)
        like_phrase_counter.update(phrases)

        for phrase, snippet in contexts:
            # Store chapter index as well as filename for spacing-based filtering later
            like_phrase_examples[phrase].append((chapter_index, file.name, snippet))

        # Cliché phrase detection within this chapter
        if phrase_patterns:
            lowered = text.lower() if CLICHE_CONFIG["case_insensitive"] else text
            for phrase, pattern in phrase_patterns:
                for m in pattern.finditer(lowered):
                    start, end = m.start(), m.end()
                    context = text[max(0, start - CLICHE_CONFIG["context_chars"]):end + CLICHE_CONFIG["context_chars"]].strip()
                    cliche_matches[phrase].append((file.name, context))

    total_words = len(full_text.split())
    total_likes = count_word_occurrences(full_text, "like")

    # Crutch word statistics (per 10k words)
    crutch_stats = {}
    if total_words > 0:
        for word, cfg in TARGET_WORDS.items():
            count = count_word_occurrences(full_text, word)
            per_10k = (count / total_words) * 10000
            crutch_stats[word] = {
                "count": count,
                "per_10k": per_10k,
                "warning_threshold": cfg.get("per_10k_warning", None),
            }

    # Aggregate 'like [noun]' patterns with spacing rules
    repeated_phrases = {}
    min_repeats = SIMILE_CONFIG.get("min_repeats_to_report", 2)
    min_gap_ok = SIMILE_CONFIG.get("min_chapter_gap_ok", 8)

    for phrase, count in like_phrase_counter.items():
        if count < min_repeats:
            continue

        occurrences = like_phrase_examples.get(phrase, [])
        chapter_indices = sorted({chap_idx for chap_idx, _, _ in occurrences})

        # If there are at least two uses closer than min_gap_ok chapters apart,
        # we treat the phrase as "repeated" and worth reporting.
        is_problematic = False
        if len(chapter_indices) >= 2:
            for a, b in zip(chapter_indices, chapter_indices[1:]):
                if (b - a) < min_gap_ok:
                    is_problematic = True
                    break

        if is_problematic:
            repeated_phrases[phrase] = count

    # Per-chapter normalised 'like' rates (per 1,000 words)
    chapter_labels = []
    chapter_like_rates = []
    for chapter_name, word_count, like_count in chapter_stats:
        chapter_labels.append(chapter_name)
        rate = (like_count / word_count) * 1000 if word_count else 0.0
        chapter_like_rates.append(rate)

    # Simile head noun counts (e.g. 'like fire', 'like stone' → heads 'fire', 'stone')
    simile_head_counts = Counter()
    for phrase, count in like_phrase_counter.items():
        parts = phrase.split(" ", 1)
        if len(parts) == 2:
            head = parts[1]
            simile_head_counts[head] += count

    # Cliché phrase aggregation
    filtered_cliches = {
        phrase: instances
        for phrase, instances in cliche_matches.items()
        if len(instances) >= CLICHE_CONFIG["min_hits_to_report"]
    }
    sorted_cliches = sorted(filtered_cliches.items(), key=lambda kv: len(kv[1]), reverse=True)

    # --- Plot: normalised 'like' rate per chapter ---
    if chapter_like_rates:
        plt.figure(figsize=(10, 5))
        x_positions = list(range(1, len(chapter_like_rates) + 1))
        plt.bar(x_positions, chapter_like_rates)
        plt.title("'Like' rate per chapter (per 1,000 words)")
        plt.xlabel("Chapter # (in file order)")
        plt.ylabel("'Like' per 1,000 words")
        plt.xticks(x_positions, [str(i) for i in x_positions], rotation=45)
        plt.tight_layout()
        plt.savefig(plot_path)
        plt.close()

    # --- Markdown Report ---
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 📊 Style Expression Report — 'Like', Crutch Words & Clichés\n_Generated: {ts}_\n\n")
        f.write(f"**Total word count:** {total_words:,}\n")
        f.write(f"**Total 'like' occurrences:** {total_likes}\n\n")
        f.write(ref_section)

        # Crutch word overview
        f.write("## 🔎 Crutch Word Overview (per 10,000 words)\n\n")
        if crutch_stats:
            f.write("| Word | Raw count | per 10k words | Threshold | Note |\n")
            f.write("|------|-----------|--------------|-----------|------|\n")
            for word, stats in sorted(crutch_stats.items(), key=lambda item: item[0]):
                count = stats["count"]
                per_10k = stats["per_10k"]
                threshold = stats["warning_threshold"]
                note = ""
                if threshold is not None and per_10k > threshold:
                    note = "⚠️ above guideline"
                f.write(f"| `{word}` | {count} | {per_10k:.1f} | {threshold or '–'} | {note} |\n")
            f.write("\n")
        else:
            f.write("_No crutch-word statistics available (no text or misconfiguration)._\n\n")

        f.write(f"![Graph]({plot_path.name})\n\n")

        SHOW_ALL_PHRASES = False
        phrases_to_show = (
            like_phrase_counter.items() if SHOW_ALL_PHRASES else repeated_phrases.items()
        )

        # --- Summary of repeated patterns ---
        f.write("## 🔁 Repeated 'like [noun]' Patterns (Normalized)\n")
        if repeated_phrases:
            for phrase, count in sorted(phrases_to_show, key=lambda x: -x[1]):
                f.write(f"- `{phrase}` — {count} times\n")
        else:
            f.write("_No repeated 'like [noun]' patterns found._\n")
        f.write("\n---\n\n")

        # --- Locations and Contexts ---
        f.write("## 📍 Locations of Repeated 'like [noun]' Phrases\n")
        if repeated_phrases:
            for phrase, count in sorted(phrases_to_show, key=lambda x: -x[1]):
                f.write(f"### 🔹 `{phrase}` — {count} uses\n\n")
                occurrences = sorted(
                    like_phrase_examples.get(phrase, []),
                    key=lambda entry: entry[0],  # sort by chapter_index
                )
                for chap_idx, fname, snippet in occurrences:
                    f.write(f"- Chapter {chap_idx} (`{fname}`): _{snippet}_\n")
                f.write("\n")
        else:
            f.write("_No matching occurrences found for repeated phrases._\n")


        # --- Cliché phrase summary & details ---
        f.write("\n---\n\n")
        f.write("## 🧾 Cliché Phrase Report\n\n")
        if not sorted_cliches:
            f.write("✅ No clichés found based on the configured list.\n")
        else:
            f.write("### 🔹 Summary Table\n\n")
            f.write("| Phrase | Matches |\n")
            f.write("|--------|---------|\n")
            for phrase, instances in sorted_cliches:
                f.write(f"| `{phrase}` | {len(instances)} |\n")

            f.write("\n---\n\n")
            f.write("### 🔍 Detailed Matches\n\n")
            for phrase, instances in sorted_cliches:
                f.write(f"#### Phrase: `{phrase}` ({len(instances)} matches)\n\n")
                for chapter, context in instances:
                    f.write(f"**{chapter}** — `{context.strip()}`\n\n")

    print(f"✅ Report written to: {report_path}")
    print(f"📈 Chart saved to: {plot_path}")

if __name__ == "__main__":
    main()
