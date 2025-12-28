#!/usr/bin/env python3
"""
## 01 Make Chunks
This script is the front door to the whole edit pipeline.  
Given a project folder, it looks for a single DOCX manuscript,  
walks through every paragraph, and does two jobs at once:

1. **It splits the book into tidy text chunks:**
   - Recognises chapter (and rule) headings, plus an optional epilogue.
   - Treats everything before the first heading as a preamble.
   - Writes out `preamble.txt` and `chapter_XXX.txt` files into a `chapters/` folder.

2. **It scans for formatting artefacts that might trip us up later:**
   - Unexpected paragraph styles, highlights, non-black text.
   - Square brackets, strikethrough, underlines, odd line-break characters.
   - Duplicate chapter numbers or an epilogue clash.

The result is a clean set of chapter files for downstream checks,  
plus a markdown report (`r_01_chunk_manuscript.md`) summarising anything suspicious  
it noticed in the original DOCX.
"""

import re, sys
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import RGBColor
from typing import Any, Dict, List, Tuple

# ——— Helpers ———

RULE_HEADING = re.compile(r'^(?:rule|chapter)\s*(?:#\s*)?(\d+)\s*[:.\-–—.]?\s*(.*)$', re.IGNORECASE)
EPILOGUE_HEADING = re.compile(r'^\s*epilogue\b[:.\-–—.]?\s*(.*)$', re.IGNORECASE)

EXPECTED_STYLES = {"Normal", "Body Text", "Heading 1", "Heading 2", "Heading 3", "Vellum Flush Left", "Vellum Text Conversation", "Vellum Chapter Title", "Vellum Verse", "Vellum Hidden Heading", "Vellum Element Subtitle", "Vellum Centered Text", "Vellum Inline Image", "Quote"}

def read_docx_lines_with_checks(path: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Read all paragraphs from a DOCX file and collect any formatting/content artefacts.

    Returns a tuple of (paragraph_texts, issues), where issues is a list of dictionaries
    describing detected artefacts.
    """
    doc = Document(path)
    issues = []
    paragraphs: List[str] = []

    for p_idx, para in enumerate(doc.paragraphs):
        paragraphs.append(para.text)
        context = para.text.strip()

        # Paragraph style check
        style_name = getattr(para.style, "name", None)
        if style_name and style_name not in EXPECTED_STYLES:
            issues.append({
                "type": "unknown_style",
                "text": context,
                "style": style_name,
                "para_idx": p_idx
            })

        for run in para.runs:
            text = run.text

            if run.font.highlight_color and run.font.highlight_color != WD_COLOR_INDEX.AUTO:
                issues.append({
                    "type": "highlight",
                    "text": text,
                    "context": context,
                    "para_idx": p_idx
                })

            color = run.font.color
            if color and color.rgb and color.rgb != RGBColor(0, 0, 0):
                issues.append({
                    "type": "non_black_text",
                    "text": text,
                    "color": str(color.rgb),
                    "context": context,
                    "para_idx": p_idx
                })

            if '[' in text or ']' in text:
                issues.append({
                    "type": "square_brackets",
                    "text": text,
                    "context": context,
                    "para_idx": p_idx
                })

            if run.font.strike:
                issues.append({
                    "type": "strikethrough",
                    "text": text,
                    "context": context,
                    "para_idx": p_idx
                })

            if run.font.underline:
                issues.append({
                    "type": "underline",
                    "text": text,
                    "context": context,
                    "para_idx": p_idx
                })

            if '\v' in text or '\u000b' in text:
                issues.append({
                    "type": "line_break",
                    "text": "\\v",
                    "context": context,
                    "para_idx": p_idx
                })

            if '"' in text and not re.search(r'[“”]', text):
                issues.append({
                    "type": "straight_quote",
                    "text": '"',
                    "context": context,
                    "para_idx": p_idx
                })

            if "'" in text and not re.search(r"[‘’]", text):
                issues.append({
                    "type": "straight_quote",
                    "text": "'",
                    "context": context,
                    "para_idx": p_idx
                })

    return paragraphs, issues


def find_docx_file(base_path: Path) -> Path:
    """
    Locate a single .docx file in the given directory.

    Raises:
        FileNotFoundError: if no .docx files are present.
        RuntimeError: if more than one .docx file is found.
    """
    candidates = list(base_path.glob("*.docx"))
    if not candidates:
        raise FileNotFoundError(f"❌ No .docx file found in: {base_path.resolve()}")
    if len(candidates) > 1:
        raise RuntimeError(f"⚠️ More than one .docx file found in: {base_path.resolve()}")
    if candidates[0].name == "fake.docx":
        raise RuntimeError(f"⚠️ fake.docx file found in: {base_path.resolve()}")
    return candidates[0]


# ——— Chapter Writer ———
def save_full_chapter(paras: List[str], chapter_number: int, chapter_title: str, chapters_dir: Path) -> None:
    """
    Save a full chapter (or preamble) to a text file in the chapters directory.

    Chapter 0 is treated as a special preamble and written to 'preamble.txt'.
    All other chapters are written to 'chapter_XXX.txt'.
    """
    if not paras:
        return
    joined = "\n\n".join(paras)  # ✅ No replacements here
    header = chapter_title.strip()
    if not header.endswith((".", "!", "?")):
        header += "."
    full_text = f"{header}\n\n{joined}"
    if chapter_number == 0:
        file_name = "preamble.txt"
    else:
        file_name = f"chapter_{chapter_number:03}.txt"
    out_path = chapters_dir / file_name
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(full_text + "\n")

# ——— Main Logic ———
def main() -> None:
    """
    Entry point: read the source DOCX, split it into chapter-sized chunks,
    and write both the chapter files and an artefact report.
    """
    if len(sys.argv) < 2:
        print("❌ Please provide the working directory path as an argument.")
        sys.exit(1)

    base_path = Path(sys.argv[1])
    if not base_path.is_dir():
        print(f"❌ Provided path is not a directory: {base_path}")
        sys.exit(1)

    input_file = find_docx_file(base_path)
    chapters_dir = base_path / "chapters"
    REPORT = base_path / "reports/r_01_chunk_manuscript.md"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    paragraphs, issues = read_docx_lines_with_checks(input_file)
    chapter_number = 0
    chapter_title = "preamble"
    chapter_paragraphs: List[str] = []
    seen_chapter_numbers: set[int] = set()

    i = 0
    while i < len(paragraphs):
        line = paragraphs[i]
        stripped = line.strip()

        m_rule = RULE_HEADING.match(stripped)
        m_epi  = EPILOGUE_HEADING.match(stripped)

        if m_rule:
            # flush previous chapter before starting a new one
            if chapter_paragraphs:
                save_full_chapter(
                    chapter_paragraphs,
                    chapter_number,
                    chapter_title,
                    chapters_dir
                )
                chapter_paragraphs = []

            # number comes directly from the Rule/Chapter heading; fall back to incrementing
            try:
                new_number = int(m_rule.group(1))
            except ValueError:
                new_number = chapter_number + 1
            else:
                if new_number in seen_chapter_numbers:
                    issues.append({
                        "type": "duplicate_chapter_number",
                        "text": stripped,
                        "para_idx": i,
                        "context": stripped,
                    })

            chapter_number = new_number
            seen_chapter_numbers.add(chapter_number)
            # keep the whole line as the title (e.g. "Rule #1: Do Not Take It to Heart")
            chapter_title = stripped
            i += 1
            continue

        if m_epi:
            # flush previous chapter before epilogue
            if chapter_paragraphs:
                save_full_chapter(
                    chapter_paragraphs,
                    chapter_number,
                    chapter_title,
                    chapters_dir
                )
                chapter_paragraphs = []

            # epilogue follows the last numbered chapter
            new_number = (chapter_number or 0) + 1
            if new_number in seen_chapter_numbers:
                issues.append({
                    "type": "duplicate_chapter_number",
                    "text": stripped if stripped else "Epilogue",
                    "para_idx": i,
                    "context": stripped if stripped else "Epilogue",
                })

            chapter_number = new_number
            seen_chapter_numbers.add(chapter_number)
            # use the full line if provided, else plain "Epilogue"
            chapter_title = stripped if stripped else "Epilogue"
            i += 1
            continue


        if stripped:
            chapter_paragraphs.append(stripped)
        i += 1


    if chapter_paragraphs:
        save_full_chapter(
            chapter_paragraphs,
            chapter_number,
            chapter_title,
            chapters_dir
        )

    # ——— Write artefact report ———
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    grouped_issues = defaultdict(list)
    for issue in issues:
        grouped_issues[issue['type']].append(issue)

    summary_counts = Counter(issue['type'] for issue in issues)

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(f"# 🧪 Artefact Report\n")
        f.write(f"_Generated: {ts}_\n\n")

        if not issues:
            f.write("✅ No artefacts detected.\n")
        else:
            f.write("## 📊 Summary\n\n")
            f.write("| Artefact Type       | Count |\n")
            f.write("|---------------------|-------|\n")
            for issue_type, count in sorted(summary_counts.items(), key=lambda x: (-x[1], x[0])):
                f.write(f"| {issue_type} | {count} |\n")
            f.write("\n")

            for issue_type, entries in grouped_issues.items():
                f.write(f"## 🔎 `{issue_type}` — {len(entries)} occurrence(s)\n\n")
                for issue in entries:
                    f.write(f"### 📍 Paragraph: {issue['para_idx']}\n")
                    f.write(f"**Text:** `{issue['text']}`\n")
                    if 'color' in issue:
                        f.write(f"**Color:** `{issue['color']}`\n")
                    if 'style' in issue:
                        f.write(f"**Style:** `{issue['style']}`\n")
                    f.write("\n**Context:**\n")
                    f.write("```text\n")
                    f.write(issue.get('context', '[no context]') + "\n")
                    f.write("```\n\n")

    print(f"🧾 Artefact report saved to {REPORT.resolve()}")
    print(f"📘 Chapters saved to {chapters_dir.resolve()}")

if __name__ == '__main__':
    main()
