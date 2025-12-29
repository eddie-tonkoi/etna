#!/usr/bin/env python3
"""
## Repetition Focus Check — what this script does

This tool looks for patterns in your gesture, reaction and intensifier verbs —
the little stylistic tics that can build up unnoticed during drafting. It isn’t
trying to enforce a limit or prescribe a style. Its only job is to highlight
chapters where one of these lemmas appears unusually often.

It works in three broad steps:

1. **Counts usage of a curated list of stylistic lemmas**
   - Loads the chapter files and parses them with spaCy.
   - Tracks how often each lemma (e.g. *smile*, *glance*, *shrug*, *just*,
     *really*, *perhaps*, *slowly*) appears in total.
   - Builds a per‑chapter breakdown so we can see where clusters happen.

2. **Applies simple thresholds to identify suspicious concentrations**
   - A lemma is only considered if it occurs at least `MIN_TOTAL_USES` times in
     the whole book.
   - Then it checks whether one chapter contains both:
     - at least `MIN_CHAPTER_USES` uses, and
     - at least `MIN_CHAPTER_SHARE` of that lemma’s total.
   - This surfaces the sections where a gesture or reaction verb may have
     become a crutch.

3. **Produces a clear markdown report**
   - Starts with a top‑level “all clear” message if nothing triggered.
   - Shows the full usage table for every lemma so you can see your stylistic
     profile at a glance.
   - Then lists any lemmas that *were* heavily concentrated and provides a
     per‑chapter breakdown for each.

It’s intentionally gentle and narrow in scope: a quick way to spot patterns in
body‑language verbs and intensifiers without drowning you in noise.

Note: unlike some other rule-based scripts, this one does not read scripts/common/config.yaml (or config.local.yaml). It only uses the folder you pass in (or `.`), and expects `chapters/` and writes `reports/` inside that folder.
"""
import argparse
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
import sys

import spacy
from tqdm import tqdm

# --- CONFIG -------------------------------------------------------------

# Lemmas we actually care about stylistically.
# (You can tweak this list as you notice your own “tics”.)
FOCUS_LEMMAS = {
    # Facial expressions / gaze
    "smile", "grin", "smirk", "beam", "grimace",
    "frown", "scowl",
    "stare", "gaze", "glare", "glance", "blink",
    "squint", "peer", "wink",

    # Laughter / crying / vocal reactions
    "laugh", "chuckle", "giggle", "scoff",
    "sob", "cry", "weep", "groan", "moan",

    # Body language / posture / movement
    "shrug", "flinch", "shiver", "tremble", "shake", "shudder",
    "tense", "relax", "slump", "straighten", "lean",
    "shift", "sway", "pace", "freeze", "still",

    # Mouth / breathing / throat
    "sigh", "huff", "sniff", "snort", "gasp", "pant",
    "breathe", "exhale", "inhale",
    "swallow", "gulp", "lick", "purse", "press",

    # Hands
    "clench", "tighten", "grip", "grasp", "fidget",
    "drum", "tap", "twist", "wring", "fold", "clasp", "rake",

    # Vocal tone / speech style
    "whisper", "murmur", "mutter", "shout", "yell",
    "snap", "bark", "hiss", "growl", "purr",
    "stammer", "stutter",

    # Adverbs / intensifiers (often overused)
    "suddenly", "quietly", "slowly", "carefully",
    "really", "very", "just", "softly", "gently",
    "almost", "nearly", "slightly", "simply",
    "actually", "probably", "maybe", "perhaps",
    "pretty", "totally", "completely", "absolutely",
    "definitely", "literally",
}

# Thresholds to keep the report focused
MIN_TOTAL_USES = 25        # only report lemmas used at least this many times in the whole book
MIN_CHAPTER_USES = 6       # and with at least this many in a single chapter
MIN_CHAPTER_SHARE = 0.25   # and that chapter has at least this fraction of all uses (e.g. 0.25 = 25%)

# -----------------------------------------------------------------------


def analyse_book(chapters_dir: Path, nlp):
    """
    Walk through all chapter .txt files, count FOCUS_LEMMAS by lemma and by chapter.
    Returns:
        total_counts: Counter[lemma] -> total uses in book
        per_chapter_counts: dict[lemma] -> Counter[chapter_name] -> count
        chapter_order: list of chapter_name in the order processed
    """
    total_counts = Counter()
    per_chapter_counts = defaultdict(Counter)
    chapter_order = []

    chapter_files = sorted(chapters_dir.glob("*.txt"))

    pbar = tqdm(
        chapter_files,
        desc="Scanning chapters",
        dynamic_ncols=True,
        file=sys.stdout,
    )

    for file in pbar:
        pbar.set_postfix_str(file.name)
        text = file.read_text(encoding="utf-8", errors="ignore")
        chapter_name = file.stem
        chapter_order.append(chapter_name)

        doc = nlp(text)
        for tok in doc:
            if not tok.is_alpha:
                continue
            lemma = tok.lemma_.lower()
            if lemma in FOCUS_LEMMAS:
                total_counts[lemma] += 1
                per_chapter_counts[lemma][chapter_name] += 1

    return total_counts, per_chapter_counts, chapter_order


def find_potential_problems(total_counts, per_chapter_counts):
    """
    Apply thresholds to find lemmas that may be over-concentrated in one chapter.
    Returns a dict: lemma -> dict with total, top_chapter, top_count, share, per_chapter Counter.
    """
    issues = {}

    for lemma, total in total_counts.items():
        if total < MIN_TOTAL_USES:
            continue

        chapter_counter = per_chapter_counts.get(lemma, Counter())
        if not chapter_counter:
            continue

        top_chapter, top_count = chapter_counter.most_common(1)[0]
        share = top_count / total if total else 0.0

        if top_count >= MIN_CHAPTER_USES and share >= MIN_CHAPTER_SHARE:
            issues[lemma] = {
                "total": total,
                "top_chapter": top_chapter,
                "top_count": top_count,
                "share": share,
                "chapters": chapter_counter,
            }

    return issues


def write_report(report_path: Path, total_counts, issues, chapter_order):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with report_path.open("w", encoding="utf-8") as f:
        f.write("# 🔁 Word Repetition Report — Focus Lemmas\n")
        f.write(f"_Generated: {ts}_\n\n")

        f.write("This report focuses on a curated set of body-language, reaction,\n")
        f.write("and intensifier lemmas. It only shows lemmas that are both frequent\n")
        f.write(f"(≥ {MIN_TOTAL_USES} uses in the book) and heavily concentrated in a single chapter\n")
        f.write(f"(≥ {MIN_CHAPTER_USES} uses **and** ≥ {int(MIN_CHAPTER_SHARE*100)}% of all uses).\n\n")

        if not issues:
            f.write("## ✅ No strongly concentrated repetitions found\n\n")
            f.write("None of the tracked lemmas exceeded the configured thresholds for concentration.\n\n")
            # Still show totals afterwards

        # Summary of all focus lemma totals (even ones that didn't trigger issues)
        if total_counts:
            f.write("## 🔹 Focus Lemma Totals (whole book)\n\n")
            f.write("| Lemma | Total uses |\n")
            f.write("|-------|------------|\n")
            for lemma, count in total_counts.most_common():
                f.write(f"| `{lemma}` | {count} |\n")
            f.write("\n")
        else:
            f.write("_No focus lemmas were found in the text._\n")
            return

        f.write("\n---\n\n")
        f.write("## ⚠️ Potentially Overused Lemmas by Chapter\n\n")
        f.write("| Lemma | Book total | Top chapter | In that chapter | Share of total |\n")
        f.write("|-------|------------|-------------|------------------|----------------|\n")
        for lemma, info in sorted(issues.items(), key=lambda x: -x[1]["total"]):
            f.write(
                f"| `{lemma}` | {info['total']} | `{info['top_chapter']}` | "
                f"{info['top_count']} | {info['share']*100:.1f}% |\n"
            )
        f.write("\n")

        # Detailed breakdown per lemma
        for lemma, info in sorted(issues.items(), key=lambda x: -x[1]["total"]):
            f.write(f"\n### Lemma: `{lemma}` — {info['total']} uses\n\n")
            f.write(
                f"- Heaviest use in **{info['top_chapter']}** "
                f"({info['top_count']} uses, {info['share']*100:.1f}% of all uses)\n\n"
            )
            f.write("Per-chapter distribution:\n\n")
            f.write("| Chapter | Count |\n")
            f.write("|---------|-------|\n")

            # Show chapters in book order, but only those where lemma appears
            chapter_counts = info["chapters"]
            for ch in chapter_order:
                if ch in chapter_counts:
                    f.write(f"| `{ch}` | {chapter_counts[ch]} |\n")
            f.write("\n")


def main():
    parser = argparse.ArgumentParser(
        description="Report on repetition of selected 'focus' lemmas (gestures, reactions, intensifiers)."
    )
    parser.add_argument("path", nargs="?", default=".", help="Path to project folder (default: current directory)")
    parser.add_argument("--chapters-dir", default="chapters", help="Folder with chunked .txt files (default: chapters)")
    parser.add_argument("--reports-dir", default="reports", help="Folder for reports (default: reports)")
    parser.add_argument(
        "--spacy-model",
        default="en_core_web_sm",
        help="spaCy model to use (default: en_core_web_sm)",
    )
    args = parser.parse_args()

    try:
        nlp = spacy.load(args.spacy_model)
    except OSError:
        print(f"❌ spaCy model '{args.spacy_model}' is not installed.")
        print(f"Install with: python -m spacy download {args.spacy_model}")
        sys.exit(1)

    target_path = Path(args.path)
    chapters_dir = target_path / args.chapters_dir
    reports_dir = target_path / args.reports_dir
    report_path = reports_dir / "r_11_repetition_patterns.md"

    if not chapters_dir.exists():
        print(f"❌ No '{args.chapters_dir}' directory at {chapters_dir}. Run your chunking step first.")
        sys.exit(1)

    total_counts, per_chapter_counts, chapter_order = analyse_book(chapters_dir, nlp)
    issues = find_potential_problems(total_counts, per_chapter_counts)
    write_report(report_path, total_counts, issues, chapter_order)

    # Terminal summary: path first, then a concise final status line.
    print(f"Report written to {report_path}")

    if not issues:
        print("✅ No strongly concentrated repetitions detected")
    else:
        top_hits = sum(info.get("top_count", 0) for info in issues.values())
        print(f"⚠️  Found {len(issues)} over-concentrated lemma(s) ({top_hits} top-chapter hit(s))")


if __name__ == "__main__":
    main()