#!/usr/bin/env python3
"""
## Compound / Hyphenation House-Style Check — what this script does

This script enforces consistency for compound words and hyphenation choices
across the book. It doesn’t decide what’s “correct English”; instead, it follows
a small configuration file of house-style preferences and reports where variants
appear.

Example families:
- `email` vs `e-mail`
- `coworker` vs `co-worker` vs `co worker`
- `timeframe` vs `time frame` vs `time-frame`

For each family, it shows how often the preferred form is used and where any
non-preferred variants appear, with chapter and line context.
"""

import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
import re
from tqdm import tqdm
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


if "compound_style_txt" not in PATHS:
    print("❌ Missing required config key: paths.compound_style_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

DEFAULT_STYLE_FILE = str(REPO_ROOT / PATHS["compound_style_txt"])


# ——— CLI ———

parser = argparse.ArgumentParser(
    description="Check consistency of compound word / hyphenation house style."
)
parser.add_argument("path", nargs="?", default=".", help="Base project folder")
parser.add_argument(
    "--chapters-dir", default="chapters", help="Folder with chunked .txt files"
)
parser.add_argument(
    "--reports-dir", default="reports", help="Folder for markdown reports"
)
parser.add_argument(
    "--style-file",
    default=None,
    help="Compound style config file. If omitted, uses paths.compound_style_txt from scripts/common/config.yaml.",
)
args = parser.parse_args()

base_path = Path(args.path)
chapters_dir = base_path / args.chapters_dir
reports_dir = base_path / args.reports_dir
report_path = reports_dir / "r_06_compound_consistency.md"
reports_dir.mkdir(parents=True, exist_ok=True)

# Resolve style file:
# - If --style-file is provided, accept absolute or relative paths.
#   For relative paths, try relative-to-project first, then relative-to-repo.
# - If omitted, use the config default (repo-relative).
if args.style_file:
    candidate = Path(args.style_file)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates = [base_path / candidate, REPO_ROOT / candidate]

    style_path = next((p for p in candidates if p.exists()), candidates[0])
else:
    style_path = Path(DEFAULT_STYLE_FILE)

if not style_path.exists():
    print(f"❌ Compound style file not found: {style_path}")
    if args.style_file:
        print("Checked project-relative and repo-relative locations.")
    else:
        print("Check paths.compound_style_txt in etna/scripts/common/config.yaml.")
    sys.exit(1)


# ——— Load style config ———

def parse_style_file(path: Path):
    """
    Parse lines of the form:
        preferred <= alt1, alt2, alt3
    ignoring blank lines and comments (# ...).
    Returns:
        style_families: dict[family_key] = {
            "preferred": preferred_form,
            "alts": [alt1, alt2, ...],
        }
    """
    families = {}
    if not path.exists():
        print(f"❌ No style file found at {path}.")
        sys.exit(1)

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "<=" not in stripped:
            print(f"⚠️ Skipping malformed style line: {stripped}")
            continue

        left, right = stripped.split("<=", 1)
        preferred = left.strip()
        alts = [a.strip() for a in right.split(",") if a.strip()]

        if not preferred or not alts:
            print(f"⚠️ Skipping malformed style line: {stripped}")
            continue

        key = preferred.lower()
        families[key] = {
            "preferred": preferred,
            "alts": alts,
        }

    return families


style_families = parse_style_file(style_path)

if not style_families:
    print(f"❌ No style families configured in: {style_path}")
    print("Add entries of the form: preferred <= alt1, alt2, alt3")
    sys.exit(1)


# Precompile regex patterns for each family/form
family_patterns = {}
for key, info in style_families.items():
    variants = [info["preferred"]] + info["alts"]
    compiled = {}
    for form in variants:
        # \b to keep to whole words; use re.IGNORECASE but keep original case in report
        pattern = re.compile(rf"\b{re.escape(form)}\b", re.IGNORECASE)
        compiled[form] = pattern
    family_patterns[key] = compiled


# ——— Scan chapters ———

# Data structure:
# per_family[family_key]["counts"][form] -> count
# per_family[family_key]["occurrences"][form] -> list of dicts
per_family = {
    key: {
        "counts": Counter(),
        "occurrences": defaultdict(list),
    }
    for key in style_families.keys()
}

if not chapters_dir.exists():
    print(f"❌ No '{args.chapters_dir}' directory at {chapters_dir}")
    sys.exit(1)

for path in tqdm(
    sorted(chapters_dir.glob("*.txt")),
    desc="Scanning chapters",
    dynamic_ncols=True,
    file=sys.stdout,
):
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    for line_no, line in enumerate(lines, start=1):
        for family_key, patterns in family_patterns.items():
            for form, pattern in patterns.items():
                # Find all non-overlapping matches
                for m in pattern.finditer(line):
                    per_family[family_key]["counts"][form] += 1
                    per_family[family_key]["occurrences"][form].append(
                        {
                            "file": path.name,
                            "line_no": line_no,
                            "context": line.strip(),
                        }
                    )


# ——— Write report ———

timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
with report_path.open("w", encoding="utf-8") as out:
    out.write("# 🧾 Compound / Hyphenation Style Report\n\n")
    out.write(f"_Generated: {timestamp}_\n\n")
    out.write(
        f"Style file: `{style_path}`\n\n"
        "Each family shows the preferred form and any alternative forms that\n"
        "actually appear in the text.\n\n"
    )

    any_issues = False

    for key, info in style_families.items():
        preferred = info["preferred"]
        alts = info["alts"]
        counts = per_family[key]["counts"]

        total_pref = counts.get(preferred, 0)
        total_any = sum(counts.values())

        if total_any == 0:
            # Nothing of this family appears at all; skip from main report
            continue

        # Collect variants that actually occur
        variants_used = [(form, counts[form]) for form in alts if counts.get(form, 0) > 0]

        # If there are no non-preferred forms, there is no issue to report for this family.
        if not variants_used:
            continue

        any_issues = True
        out.write("---\n\n")
        out.write(f"## `{preferred}` (family)\n\n")
        out.write(f"- Preferred: `{preferred}` — {total_pref} use(s)\n")

        out.write("- Variants:\n")
        for form, c in variants_used:
            out.write(f"  - `{form}` — {c} use(s)\n")

        out.write("\n")

        # Show contexts for non-preferred forms
        out.write("### Variant contexts\n\n")
        occurrences = per_family[key]["occurrences"]
        for form, _ in variants_used:
            out.write(f"**`{form}`:**\n\n")
            for occ in occurrences[form]:
                out.write(
                    f"- {occ['file']} L{occ['line_no']}: {occ['context']}\n"
                )
            out.write("\n")

    if not any_issues:
        out.write("✅ No compound/ hyphenation inconsistencies found in the text.\n")

# Terminal summary: path first, then a concise final status line.
issue_families = 0
variant_hits = 0

for key, info in style_families.items():
    alts = info["alts"]
    counts = per_family[key]["counts"]

    total_any = sum(counts.values())
    if total_any == 0:
        continue

    variants_used = [(form, counts[form]) for form in alts if counts.get(form, 0) > 0]
    if variants_used:
        issue_families += 1
        variant_hits += sum(c for _, c in variants_used)

print(f"Report written to {report_path}")

if issue_families == 0:
    print("✅ No compound/hyphenation inconsistencies detected")
else:
    print(f"⚠️  Found {issue_families} compound family issue(s) ({variant_hits} variant use(s))")