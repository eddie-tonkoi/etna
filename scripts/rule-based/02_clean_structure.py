#!/usr/bin/env python3
"""
## Structure Check — What this script does

This tool takes the cleaned chapter files produced earlier in the pipeline and
scans them for structural oddities — the small, easily overlooked gremlins that
tend to slip in during drafting or heavy editing.

It works line‑by‑line, looking for things such as:

- accidental double spaces or duplicated words
- punctuation that’s gotten a bit enthusiastic (!!!, ……, ?!?), unless it’s inside dialogue
- stray editing markers like [[ or NOTE:
- mismatched quotes, both straight and smart
- bad spacing around hyphens and dashes
- ellipsis misuse in all its creative forms
- non‑British date formats, time abbreviations, or title styles
- unexpected contraction starters or suspicious apostrophes

It also checks each line against your accepted structure list (`struct_book.txt`)
so known, intentional patterns won’t be flagged.

The output is a tidy markdown report grouping each issue type, showing where it
appears and the exact line so you can scan through quickly. It’s not trying to
be clever — just relentlessly consistent.
"""
import argparse
from pathlib import Path
import re
from datetime import datetime
from collections import Counter
import sys

# Allow importing shared config loader from scripts/common/common.py
sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
from common import load_paths  # noqa: E402

# ——— CLI Setup ———
parser = argparse.ArgumentParser(description="Check text chunks for structural anomalies.")
parser.add_argument("path", nargs="?", default=".", help="Path to folder (default: current directory)")
args = parser.parse_args()

target_path = Path(args.path)
chunk_dir = target_path / "chapters"
output_path = target_path / "reports" / "r_02_clean_structure.md"
output_path.parent.mkdir(parents=True, exist_ok=True)


# Global dictionary moved out of the per-book folder.
# Configured in scripts/common/config.yaml (optionally overlaid by config.local.yaml).
_repo_root = Path(__file__).resolve().parents[2]
_paths = load_paths()

if "dict_custom_txt" not in _paths:
    print("❌ Missing required config key: paths.dict_custom_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

GLOBAL_DICT = _repo_root / _paths["dict_custom_txt"]
if not GLOBAL_DICT.exists():
    print(f"❌ Global dictionary not found: {GLOBAL_DICT}")
    print("Check paths.dict_custom_txt in etna/scripts/common/config.yaml.")
    sys.exit(1)

ACCEPTED_STRUCT = target_path / "struct_book.txt"
DICT_BOOK = target_path / "dict_book.txt"

if not DICT_BOOK.exists():
    print(f"❌ Per-book dictionary not found: {DICT_BOOK.resolve()}")
    sys.exit(1)

# Load accepted lines
accepted_lines = set()
if ACCEPTED_STRUCT.exists():
    with open(ACCEPTED_STRUCT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                accepted_lines.add(line)

# ——— Patterns ———
double_space = re.compile(r"  +")
repeated_word = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
multi_punct = re.compile(
    r"""
    (?<!\w)                # avoid matching inside words
    (
        [!?]{3,}           # e.g. !!!, ???, ?!?, !?!
        | \.{4,}           # 4 or more dots
    )
    """,
    re.VERBOSE
)
line_end_comma = re.compile(r'.+,\s*$')
terminal_punct = re.compile(r"[.!?…,:;—–-]$")
word_token = re.compile(r"[A-Za-z0-9]+(?:[’'][A-Za-z0-9]+)?")
QUOTE_START = re.compile(r'^\s*[“"\'‘]')

#scene_breaks = re.compile(r"^([-–—\*#~_]{3,})$")
editing_tags = re.compile(r"\[\[|<<|NOTE:")
split_year = re.compile(r"\b(19|20)\s{1,2}[0-9]{2}\b")
hyphen_spacing = re.compile(r"\b\w+\s+-\s+\w+\b")
#hyphenated_word = re.compile(r"\b\w+-\w+\b")
emdash_spacing = re.compile(r"\s—\s|\s—|—\s")

endash_range = re.compile(r"\b[0-9A-Za-z]+\s*–\s*[0-9A-Za-z]+\b")
endash_break = re.compile(r"\b\w+\s*–\s*\w+\b")
bad_ellipsis = re.compile(
    r"""
    \.\.\.             # plain three-dot ellipsis
    | [.]…             # dot before ellipsis
    | …[.]             # dot after ellipsis
    | …{2,}            # two or more ellipsis characters
    | …\s*…+           # ellipsis separated by optional space(s)
    """,
    re.VERBOSE
)

british_titles_with_period = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Prof|Rev|Gen|Gov|Hon|Pres|Sen|Lt|Col|Maj|Capt|Cmdr|Adm|Sgt|Cpl)\.", re.IGNORECASE)
time_with_periods = re.compile(r"\b\d{1,2}\s?(a\.m\.|p\.m\.)\b", re.IGNORECASE)
amid_usage = re.compile(r"\bamid\b", re.IGNORECASE)
non_british_date = re.compile(
    r"""
    \b(             # Start of word boundary
        (?:January|February|March|April|May|June|July|
           August|September|October|November|December)   # Month name
        \s+\d{1,2}(?:st|nd|rd|th)?                       # Followed by ordinal day
        ,\s+\d{4}                                        # Comma + 4-digit year
    |
        \d{1,2}/\d{1,2}/\d{2,4}                          # Slash-separated date
    |
        (?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day       # Day name
        ,?\s+(?:January|February|March|April|May|June|
                July|August|September|October|November|December)
        \s+\d{1,2}(?:st|nd|rd|th)?                       # Month + day
    )
    \b
    """,
    re.VERBOSE | re.IGNORECASE
)

contraction_starters = [
    "Cause", "Twas", "Tis", "Em", "Bout", "Round", "Til", "Fore", "Neath", "Tweren’t", "Twasn’t"
]
square_brackets = re.compile(r"[\[\]]")
bad_contraction_regex = re.compile(
    r"\b‘(?:{})\b".format("|".join(re.escape(word) for word in contraction_starters))
)
def load_dictionary(paths):
    entries = set()
    for path in paths:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    word = line.strip()
                    if word:
                        entries.add(word.lower())
    return entries

def is_inside_quotes(pos, line):
    quotes_before = line[:pos].count("‘") + line[:pos].count("’")
    quotes_before += line[:pos].count("“") + line[:pos].count("”")
    quotes_before += line[:pos].count('"') + line[:pos].count("'")
    return quotes_before % 2 == 1

def strip_trailing_closers(s: str) -> str:
    """Strip trailing closing quotes/brackets so terminal punctuation can be detected."""
    s = s.rstrip()
    while s and s[-1] in ("”", "’", '"', "'", ")", "]", "}"):
        s = s[:-1].rstrip()
    return s

def run_checks(text, custom_entries):
    issues = []
    lines = text.splitlines()

    last_para_line_no = None
    last_para_line = None
    para_word_count = 0

    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()

        # Paragraph boundary: blank line separates paragraphs.
        if not stripped:
            if last_para_line is not None and para_word_count >= 10:
                last_stripped = last_para_line.strip()
                if last_stripped not in accepted_lines:
                    candidate = strip_trailing_closers(last_para_line.strip())
                    if candidate and not terminal_punct.search(candidate):
                        issues.append(("Paragraph ends without terminal punctuation", last_para_line_no, last_para_line))
            last_para_line_no = None
            last_para_line = None
            para_word_count = 0
            continue

        # Treat accepted structural lines as boundaries and skip checks.
        if stripped in accepted_lines:
            if last_para_line is not None and para_word_count >= 10:
                last_stripped = last_para_line.strip()
                if last_stripped not in accepted_lines:
                    candidate = strip_trailing_closers(last_para_line.strip())
                    if candidate and not terminal_punct.search(candidate):
                        issues.append(("Paragraph ends without terminal punctuation", last_para_line_no, last_para_line))
            last_para_line_no = None
            last_para_line = None
            para_word_count = 0
            continue

        # Track the last non-empty line in the current paragraph.
        last_para_line_no = line_no
        last_para_line = line
        para_word_count += len(word_token.findall(stripped))

        if double_space.search(line):
            issues.append(("Double space", line_no, line))

        for match in repeated_word.finditer(line):
            word = match.group(1)
            phrase = f"{word} {word}".lower()
            if phrase not in custom_entries:
                issues.append((f"Repeated word: {word}", line_no, line))

        for match in multi_punct.finditer(line):
            start, end = match.span()
            if end < len(line) and line[end] in ("’", "'", "”", '"'):
                continue
            if is_inside_quotes(start, line):
                continue
            issues.append(("Multiple punctuation", line_no, line))
            break

        if line_end_comma.match(line):
            if line_no < len(lines):
                next_line = lines[line_no]
            else:
                next_line = ""

            # Accept if next line starts with a quote or conjunction
            if not QUOTE_START.match(next_line) and not re.match(r'^\s*(?:and|but|so|or|then|yet)\b', next_line, re.IGNORECASE):
                issues.append(("Line ends in comma (suspicious dialogue/action tag?)", line_no, line))
        if editing_tags.search(line):
            issues.append(("Editing marker (e.g. [[, <<, NOTE:)", line_no, line))
        if square_brackets.search(line):
            issues.append(("Square bracket found (possible editorial note or placeholder?)", line_no, line))
        if split_year.search(line):
            issues.append(("Suspicious year format (e.g. 20 22)", line_no, line))
        if hyphen_spacing.search(line):
            issues.append(("Hyphen should not be spaced (use 'word-word', not 'word - word')", line_no, line))

        # Hyphenated words (no spaces) — surface for manual review unless explicitly whitelisted in dictionaries.
        # for match in hyphenated_word.finditer(line):
        #     hyph_word = match.group(0)
        #     if hyph_word.lower() not in custom_entries:
        #         issues.append(("Hyphenated word (review for clause break or style)", line_no, line))
        #         break

        # En dash checks: discourage en dashes as clause breaks; prefer em dashes instead.
        for match in endash_break.finditer(line):
            span = match.group(0)
            # Allow numeric or alphanumeric ranges like 1999–2003, A–Z
            if endash_range.fullmatch(span.strip()):
                continue
            issues.append(("En dash found — expected em dash", line_no, line))
            break

        # Em dash spacing: em dashes should be closed up (word—word), not spaced.
        if emdash_spacing.search(line):
            issues.append(("Em dash spacing inconsistency (should be closed up, e.g. 'word—word')", line_no, line))
        # Ellipsis form and spacing checks
        if bad_ellipsis.search(line):
            issues.append(("Bad ellipsis construction (use a single …, not ... or repeated ellipses)", line_no, line))

        # Spacing rules around the single ellipsis character:
        # Allowed:
        #   word… word
        #   word…, word
        #   word…
        #   …word  (at start of line/paragraph, no space after)
        # Disallowed examples:
        #   word…word
        #   word …word
        #   word … word
        #   … word (at start of line/paragraph)
        if "…" in line:
            for m in re.finditer("…", line):
                pos = m.start()
                before = line[:pos]
                after = line[pos + 1:]

                prev_char = line[pos - 1] if pos > 0 else ""
                next_char = line[pos + 1] if pos + 1 < len(line) else ""

                # Case: line/paragraph starts with ellipsis (only whitespace and possibly opening quotes before)
                before_stripped = before.strip()
                # Treat as start-of-line if there is nothing but optional opening quotes before the ellipsis.
                if before_stripped == "" or before_stripped.strip('“”"\'‘’') == "":
                    # Disallow '… word' or '“… word' (ellipsis then space then word)
                    if next_char == " ":
                        issues.append(("Ellipsis at line start should be '…word' (or '\"…word') with no space after", line_no, line))
                        break
                    # Otherwise allowed, e.g. '…word', '“…word', '…—', '…“'
                    continue

                # From here on, there is some non-space content before the ellipsis.

                # Disallow a space immediately before the ellipsis (e.g. 'word …word', 'word … word').
                # At paragraph start we've already early-returned above.
                if prev_char.isspace():
                    issues.append(("Space before ellipsis is not allowed (use 'word… word', not 'word … word')", line_no, line))
                    break

                # If ellipsis is the last character on the line, that's fine (e.g. 'word…').
                if pos + 1 >= len(line):
                    continue

                # Handle character(s) after ellipsis.
                if next_char == " ":
                    # 'word… word' (space then word) is allowed.
                    # If it's 'word… ' with nothing after, we accept and let other spacing rules deal with any trailing spaces.
                    continue

                if next_char == ",":
                    # Allow 'word…, word' pattern (comma + space).
                    if len(after) >= 2 and after[1] == " ":
                        continue
                    # Comma not followed by space is suspicious.
                    issues.append(("Comma after ellipsis should usually be followed by a space (use 'word…, word')", line_no, line))
                    break

                # If the next char is closing punctuation, we allow:
                # e.g. 'word…”', 'word…?' etc.
                if next_char in ("’", "”", '"', ")", "]", "}", "!", "?", "—", "–", ":", ";"):
                    continue

                # Otherwise we have something like 'word…word', which should be spaced.
                issues.append(("Ellipsis should be followed by a space or punctuation (use 'word… word', not 'word…word')", line_no, line))
                break
        if british_titles_with_period.search(line):
            issues.append(("Title abbreviation with period (use UK style: 'Mr', not 'Mr.')", line_no, line))
        if amid_usage.search(line):
            issues.append(("‘Amid’ used – consider ‘amidst’ for UK literary tone (unless ‘amid’ suits the rhythm better)", line_no, line))
        if time_with_periods.search(line):
            issues.append(("Time abbreviation with periods (use 'am' or 'pm' instead)", line_no, line))
        if non_british_date.search(line):   
            issues.append(("Date with non-British style (use '20 June 2025', not 'June 20, 2025')", line_no, line))

        match = bad_contraction_regex.search(line)
        if match:
            issues.append((
                f"Opening quote used in contraction: '{match.group(0)}' (should be apostrophe)", 
                line_no, 
                line
            ))

        single_quotes = 0
        double_quotes = 0
        for idx, ch in enumerate(line):
            if ch == "’":
                # Treat as apostrophe (not a quote) when it clearly starts or sits inside a word/number,
                # e.g. don't, 'em, '80s, hangin', etc.
                if idx < len(line) - 1 and line[idx + 1].isalnum():
                    continue
                single_quotes += 1
            elif ch == "‘":
                single_quotes += 1
            elif ch in ("“", "”"):
                double_quotes += 1

        if single_quotes % 2 != 0:
            issues.append(("Unmatched smart single quotes (odd number on line)", line_no, line))
        if double_quotes % 2 != 0:
            issues.append(("Unmatched smart double quotes (odd number on line)", line_no, line))

        straight_quotes = line.count('"') + line.count("'")
        if straight_quotes % 2 != 0:
            issues.append(("Unmatched straight quotes (odd number on line)", line_no, line))

    # End-of-file paragraph check (in case the file doesn't end with a blank line).
    if last_para_line is not None and para_word_count >= 10:
        last_stripped = last_para_line.strip()
        if last_stripped not in accepted_lines:
            candidate = strip_trailing_closers(last_para_line.strip())
            if candidate and not terminal_punct.search(candidate):
                issues.append(("Paragraph ends without terminal punctuation", last_para_line_no, last_para_line))
    return issues

def main():
    custom_entries = load_dictionary([DICT_BOOK, GLOBAL_DICT])
    if not chunk_dir.exists():
        print(f"❌ No chunks found in {chunk_dir}")
        return

    all_issues = []
    chunk_files = sorted(chunk_dir.glob("*.txt"))
    for file in chunk_files:
        text = file.read_text(encoding="utf-8")
        issues = run_checks(text, custom_entries)
        for kind, line, context in issues:
            all_issues.append({
                "file": file.name,
                "type": kind,
                "line": line,
                "context": context
            })

    all_issues.sort(key=lambda x: (x["type"], x["file"], x["line"]))

    # Count how many times each issue type appears (used for headings + terminal summary)
    issue_counts = Counter(issue["type"] for issue in all_issues)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# 🧾 Structure Check Report\n")
        f.write(f"_Generated: {timestamp}_\n\n")

        current_type = None
        for issue in all_issues:
            if issue["type"] != current_type:
                current_type = issue["type"]
                count = issue_counts[current_type]
                f.write(f"\n## 🔹 {current_type} — {count} instance{'s' if count != 1 else ''}\n")

            f.write(f"### `{issue['file']}:{issue['line']}`\n\n")
            f.write("```text\n")
            f.write(issue["context"].strip() + "\n")
            f.write("```\n\n")


    # Terminal summary: path first, then a concise final status line.
    total = len(all_issues)
    kinds = len(issue_counts)

    print(f"Report written to {output_path}")

    if total == 0:
        print("✅ No structural issues detected")
    else:
        print(f"⚠️  Found {total} structural issue(s) across {kinds} type(s)")

if __name__ == "__main__":
    main()
