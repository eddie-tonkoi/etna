#!/usr/bin/env python3
"""
## Grammar Check — what this script does

This script takes the chunked chapter files and runs a grammar and style pass
using a local LanguageTool server, then layers a few house-style rules on top.
It’s there to surface patterns and likely problems, not to enforce perfection.

It does a few main things:

1. **Starts and talks to LanguageTool**
   - Spins up a local `languagetool-server.jar` on port 8081 (and shuts it down
     cleanly on exit).
   - Uses the `en-GB` ruleset via `language_tool_python` pointed at that server.
   - Runs every chapter file in `chapters/` through LanguageTool to get grammar,
     typography and style matches.

2. **Loads suppressions to avoid nagging about known exceptions**
   - Reads suppression lists from `grammar_book.txt` and the global suppression file configured as `paths.grammar_custom_txt` in `scripts/common/config.yaml` (book file optional; global is required).
   - Uppercase lines are treated as LanguageTool rule IDs to disable completely.
   - Other lines are treated as phrases: if a match’s context contains one of these, it’s skipped.
   - Optionally, the LanguageTool `ruleIssueType` could be filtered via
     `ISSUE_TYPE_WHITELIST`, but by default all issue types are allowed.

3. **Applies house-style rules for directions and spelling**
   - `HOUSE_DIRECTIONS` enforces your preference for forms like `towards`,
     `forwards`, `afterwards` instead of their bare `-ward` cousins.
   - `HOUSE_SPELLING` nudges common AmE spellings to your chosen BrE forms
     (colour/colouring, organise, traveller, kerb, pyjamas, grey, etc.).
   - These checks:
     - run directly over the raw text for each chunk,
     - preserve the original capitalisation,
     - and won’t double-report if LanguageTool has already flagged the same spot.

4. **Normalises noisy variants of the same rule**
   - LanguageTool has several flavours of a comma-compound rule
     (`COMMA_COMPOUND_SENTENCE`, `_2`, `_3`).
   - For each context, the script keeps only the “strongest” version according
     to a small priority table, so you don’t see the same sentence three times
     with slightly different rule IDs.

5. **Writes a markdown report grouped by rule**
   - The report `r_04_grammar.md` starts with a summary table:
     - each LanguageTool rule ID and how often it was triggered.
   - Then, for each rule:
     - shows the rule’s message, category and issue type (where available),
     - lists every occurrence with:
       - the chunk filename,
       - the suggested replacement (if any),
       - and a code block with the surrounding context.
   - For `COMMA_COMPOUND_SENTENCE`, it goes a step further and groups the
     matches by conjunction (`but`, `so`, `and`, `other`) so you can skim your
     comma splices in a more organised way.

The overall goal is to collect “points of interest” for a human pass: things
worth re-reading with your editorial hat on, without drowning you in duplicate
or obviously intentional patterns.
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime
import re
import subprocess
import atexit
import time
from tqdm import tqdm
try:
    import language_tool_python
except ModuleNotFoundError as e:
    raise SystemExit(
        "Optional dependency missing: 'language_tool_python' (GPLv3).\n"
        "This script uses it as a client to talk to a running LanguageTool server.\n\n"
        "To enable this script, install the optional requirements:\n"
        "  pip install -r requirements-gpl.txt\n\n"
        "If you prefer to avoid GPL-licensed Python dependencies, you can ignore this script."
    ) from e

# ——— CLI Setup ———
parser = argparse.ArgumentParser(description="Check grammar in pre-chunked text files.")
parser.add_argument("path", nargs="?", default=".", help="Path to folder (default: current directory)")
args = parser.parse_args()

target_path = Path(args.path)
print(f"🔍 Running grammar check on: {target_path.resolve()}")

# ---------------- Config (mandatory for house_rules paths) ----------------
# Allow importing shared config loader from scripts/common/common.py
REPO_ROOT = Path(__file__).resolve().parents[2]
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

BOOK_SUPPRESSIONS = target_path / "grammar_book.txt"

if "grammar_custom_txt" not in PATHS:
    print("❌ Missing required config key: paths.grammar_custom_txt")
    print("Add it to etna/scripts/common/config.yaml (repo-relative path).")
    sys.exit(1)

GLOBAL_SUPPRESSIONS = REPO_ROOT / PATHS["grammar_custom_txt"]
if not GLOBAL_SUPPRESSIONS.exists():
    print(f"❌ Global suppression file not found: {GLOBAL_SUPPRESSIONS.resolve()}")
    print("Check paths.grammar_custom_txt in etna/scripts/common/config.yaml.")
    sys.exit(1)

# Optional sanity-check: the XML is typically installed inside the LanguageTool folder,
# so we don't try to install/use it here, but we can still verify the configured file exists.
if "grammar_custom_xml" in PATHS:
    xml_path = REPO_ROOT / PATHS["grammar_custom_xml"]
    if not xml_path.exists():
        print(f"❌ Configured grammar_custom_xml not found: {xml_path.resolve()}")
        print("Check paths.grammar_custom_xml in etna/scripts/common/config.yaml.")
        sys.exit(1)

REPORT = target_path / "reports/r_07_grammar_check.md"



# ——— House Style Directional Forms ———
HOUSE_DIRECTIONS_ENABLED = False
HOUSE_DIRECTIONS = {
    # General directional adverbs
    "forward": "forwards",
    "backward": "backwards",
    "upward": "upwards",
    "downward": "downwards",
    "inward": "inwards",
    "outward": "outwards",
    "onward": "onwards",
    "homeward": "homewards",
    "northward": "northwards",
    "southward": "southwards",
    "eastward": "eastwards",
    "westward": "westwards",
    "afterward": "afterwards",
    # Clear AmE → BrE preference
    "toward": "towards",
}

# ——— House Style Spelling Preferences ———
HOUSE_SPELLING_ENABLED = True
HOUSE_SPELLING = {

    # ——— -ise over -ize ———
    "organize": "organise",
    "organizing": "organising",
    "organizes": "organises",
    "organized": "organised",

    "recognize": "recognise",
    "recognizing": "recognising",
    "recognizes": "recognises",
    "recognized": "recognised",

    "realize": "realise",
    "realizing": "realising",
    "realizes": "realises",
    "realized": "realised",

    "emphasize": "emphasise",
    "emphasizing": "emphasising",
    "emphasizes": "emphasises",
    "emphasized": "emphasised",

    "authorize": "authorise",
    "authorizing": "authorising",
    "authorized": "authorised",
    "authorizes": "authorises",

    # ——— -or → -our ———
    "color": "colour",
    "colors": "colours",
    "colored": "coloured",
    "coloring": "colouring",

    "honor": "honour",
    "honors": "honours",
    "honored": "honoured",
    "honoring": "honouring",

    "favor": "favour",
    "favors": "favours",
    "favored": "favoured",
    "favoring": "favouring",

    "odor": "odour",
    "odors": "odours",

    "humor": "humour",
    "humors": "humours",
    "humored": "humoured",
    "humoring": "humouring",

    "labor": "labour",
    "laboring": "labouring",
    "labored": "laboured",
    "labors": "labours",

    "neighbor": "neighbour",
    "neighbors": "neighbours",
    "neighboring": "neighbouring",
    "neighbored": "neighboured",

    "armor": "armour",
    "armored": "armoured",
    "armoring": "armouring",

    "rumor": "rumour",
    "rumors": "rumours",

    "savor": "savour",
    "savory": "savoury",

    # ——— -er → -re ———
    "center": "centre",
    "centers": "centres",

    "meter": "metre",
    "meters": "metres",

    "fiber": "fibre",
    "fibers": "fibres",

    "liter": "litre",
    "liters": "litres",

    "theater": "theatre",
    "theaters": "theatres",

    "scepter": "sceptre",
    "sepulcher": "sepulchre",

    # ——— -ense → -ence ———
    "defense": "defence",
    "offense": "offence",
    "pretense": "pretence",

    # ——— -og → -ogue ———
    "dialog": "dialogue",
    "catalog": "catalogue",
    "analog": "analogue",
    "monolog": "monologue",
    "travelog": "travelogue",

    # ——— Doubled consonants ———
    "canceled": "cancelled",
    "canceling": "cancelling",
    "traveler": "traveller",
    "travelers": "travellers",
    "traveling": "travelling",
    "modeled": "modelled",
    "modeling": "modelling",
    "fueling": "fuelling",
    "fueled": "fuelled",

    "counselor": "counsellor",
    "jeweler": "jeweller",
    "marvelous": "marvellous",

    # ——— Miscellaneous common differences ———
    "program": "programme except code",  # for TV/arts; code stays 'program'
    # "check": "cheque",  # financial only
    "pajamas": "pyjamas",
    "pajama": "pyjama",
    "mustache": "moustache",
    "curb": "kerb for pavement edge",  # pavement edge only
    "tire": "tyre for wheel",  # wheel only
    "gray": "grey",
    "aluminum": "aluminium",
    "draft": "draught for airflow",  # beer/airflow sense
    "plow": "plough",
    "aging": "ageing",
    "encyclopedia": "encyclopaedia",
    "maneuver": "manoeuvre",
    "archeology": "archaeology",
    "artifact": "artefact",
    "mold": "mould",
    "sulfur": "sulphur",
    "ax": "axe",
    "jeweled": "jewelled",
    "enroll": "enrol",
    "enrollment": "enrolment",
    "whiskey": "whisky except Irish whiskey",  # except Irish whiskey

    # ——— Past participle differences sometimes flagged ———
    # "dreamed": "dreamt for past particle",
    # "learned": "learnt for past particle",
    # "burned": "burnt for past particle",
    # "spoiled": "spoilt for past particle",

    # Note: context-dependent pairs like practise/practice (verb/noun)
    # and licence/license are left to LanguageTool's grammar rules
    # rather than enforced blindly here.
}

# ——— Issue Type Handling (LanguageTool ruleIssueType) ———
# If ALLOW_ALL_ISSUE_TYPES is True, we do not filter by ruleIssueType.
# To restrict later, set this to False and populate ISSUE_TYPE_WHITELIST.
ALLOW_ALL_ISSUE_TYPES = True
ISSUE_TYPE_WHITELIST = {
    # Examples of common LanguageTool issue types; currently unused while ALLOW_ALL_ISSUE_TYPES is True.
    "grammar",
    "typographical",
    "style",
    "uncategorized",
}


# ——— Start LanguageTool Server ———
LT_PORT = int(os.environ.get("ETNA_LANGUAGETOOL_PORT", "8081"))
LT_URL = f"http://localhost:{LT_PORT}"

lt_process = None  # LanguageTool server subprocess handle

def resolve_languagetool_jar() -> Path:
    """Resolve the LanguageTool server JAR.

    Priority:
      1) ETNA_LANGUAGETOOL_SERVER_JAR (env)
      2) LANGUAGETOOL_SERVER_JAR (env, backwards compatible)
      3) scripts/common/config.yaml keys (a, b, c):
         - paths.languagetool_server_jar_a
         - paths.languagetool_server_jar_b
         - paths.languagetool_server_jar_c

    Config paths may be absolute or repo-relative.
    """
    checked: list[Path] = []

    for env_key in ("ETNA_LANGUAGETOOL_SERVER_JAR", "LANGUAGETOOL_SERVER_JAR"):
        env_val = os.environ.get(env_key)
        if env_val:
            p = Path(env_val).expanduser()
            checked.append(p)
            if p.exists():
                return p

    for key in (
        "languagetool_server_jar_a",
        "languagetool_server_jar_b",
        "languagetool_server_jar_c",
    ):
        cfg_val = PATHS.get(key)
        if not cfg_val:
            continue
        p = Path(str(cfg_val)).expanduser()
        if not p.is_absolute():
            p = REPO_ROOT / p
        checked.append(p)
        if p.exists():
            return p

    print("❌ LanguageTool server jar not found.")
    print("Checked these locations:")
    for p in checked:
        print(f"  - {p}")
    print("\nFix options:")
    print("  1) Set env var ETNA_LANGUAGETOOL_SERVER_JAR (or LANGUAGETOOL_SERVER_JAR) to the full path of languagetool-server.jar")
    print("  2) Or add one or more of these to etna/scripts/common/config.yaml:")
    print("     paths:")
    print("       languagetool_server_jar_a: \"/full/path/to/languagetool-server.jar\"")
    print("       languagetool_server_jar_b: \"/another/path/to/languagetool-server.jar\"")
    print("       languagetool_server_jar_c: \"path/to/languagetool-server.jar\"  # repo-relative ok")
    sys.exit(1)

def start_languagetool_server():
    global lt_process
    jar_path = resolve_languagetool_jar()

    lt_root = jar_path.parent  # Directory that contains the JAR and org/languagetool/...
    classpath = f"{lt_root}:{jar_path}"  # On macOS/Linux, use ':' to separate entries

    print("🚀 Starting LanguageTool server...")
    lt_process = subprocess.Popen(
        [
            "java",
            "-cp",
            classpath,
            "org.languagetool.server.HTTPServer",
            "--port",
            str(LT_PORT),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait up to 15 seconds for it to respond
    import requests
    for i in range(15):
        try:
            response = requests.get(f"{LT_URL}/v2/languages", timeout=1)
            if response.status_code == 200:
                print("✅ LanguageTool server is ready.")
                return
        except Exception:
            time.sleep(1)

    # If we get here, it failed to start — dump stderr
    print("❌ LanguageTool server failed to start.")

    err_text = ""
    if lt_process:
        try:
            _, err = lt_process.communicate(timeout=2)
            err_text = (err or "").strip()
            if err_text:
                print("🧾 Server output:\n", err_text)
        except Exception:
            print("🧾 Server output could not be read (timeout or other error).")
        finally:
            lt_process.terminate()

    # If the port is bound but the health-check failed, explain how to find/kill the listener.
    if (
        "Address already in use" in err_text
        or "PortBindingException" in err_text
        or "BindException" in err_text
    ):
        print("")
        print(
            f"ℹ️  Port {LT_PORT} is already in use, and the service listening at {LT_URL} did not respond as a LanguageTool server."
        )
        print("To identify what is using the port:")
        print(f"  lsof -nP -iTCP:{LT_PORT} -sTCP:LISTEN")
        print("To stop it:")
        print("  kill <PID>")
        print("If it refuses:")
        print("  kill -9 <PID>")
        print("")
        print("Then re-run this script (or choose another port with --port).")
        sys.exit(1)

    # Generic guidance (no extra fallbacks)
    print("")
    print("Fix options:")
    print(f"  - check whether something is already using the port: lsof -nP -iTCP:{LT_PORT} -sTCP:LISTEN")
    print(f"  - or re-run with a different port: --port {LT_PORT + 1}")
    sys.exit(1)

def stop_languagetool_server():
    global lt_process
    if lt_process:
        print("🛑 Stopping LanguageTool server...")
        lt_process.terminate()

# Register the shutdown hook and start the server
atexit.register(stop_languagetool_server)
start_languagetool_server()

# ——— Helpers ———
# Loads suppressions (ignored phrases and disabled rule IDs) from the given files
def load_suppressions(files):
    entries = set()
    for file in files:
        if file.exists():
            with open(file, encoding="utf-8") as f:
                entries |= {line.strip() for line in f if line.strip() and not line.startswith("#")}
    return entries

def find_sentence_context(text, offset, length):
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    for sent in sentences:
        if text.find(sent) <= offset < text.find(sent) + len(sent):
            return sent.strip()
    return text[max(0, offset - 60):offset + length + 60].strip()

def check_grammar(text, tool, disabled_rules, ignored_phrases):
    matches = tool.check(text)
    issues = []
    flagged_offsets = {m.offset for m in matches}

    # House-style checks for directional forms (-ward vs -wards)
    if HOUSE_DIRECTIONS_ENABLED:
        for wrong, preferred in HOUSE_DIRECTIONS.items():
            for match in re.finditer(rf"\b{re.escape(wrong)}\b", text, flags=re.IGNORECASE):
                offset = match.start()
                if offset in flagged_offsets:
                    # LanguageTool has already flagged something here, don't double-report
                    continue
                original = match.group(0)
                # Preserve capitalisation pattern of the original
                if original.isupper():
                    suggestion = preferred.upper()
                elif original[0].isupper():
                    suggestion = preferred.capitalize()
                else:
                    suggestion = preferred
                ctx = find_sentence_context(text, offset, len(original))
                issues.append({
                    "type": "house_direction",
                    "rule": "HOUSE_DIRECTION",
                    "message": f"House style: prefer '{suggestion}' instead of '{original}'.",
                    "suggestion": suggestion,
                    "context": ctx
                })

    # House-style checks for spelling preferences (e.g. realise/realize)
    if HOUSE_SPELLING_ENABLED:
        for wrong, preferred in HOUSE_SPELLING.items():
            for match in re.finditer(rf"\b{re.escape(wrong)}\b", text, flags=re.IGNORECASE):
                offset = match.start()
                if offset in flagged_offsets:
                    # LanguageTool has already flagged something here, don't double-report
                    continue
                original = match.group(0)
                # Preserve capitalisation pattern of the original
                if original.isupper():
                    suggestion = preferred.upper()
                elif original[0].isupper():
                    suggestion = preferred.capitalize()
                else:
                    suggestion = preferred
                ctx = find_sentence_context(text, offset, len(original))
                issues.append({
                    "type": "house_spelling",
                    "rule": "HOUSE_SPELLING",
                    "message": f"House style: prefer '{suggestion}' instead of '{original}'.",
                    "suggestion": suggestion,
                    "context": ctx
                })

    for m in matches:
        # Optional: filter by LanguageTool issue type if configured
        if not ALLOW_ALL_ISSUE_TYPES and m.ruleIssueType not in ISSUE_TYPE_WHITELIST:
            continue
        if m.ruleId in disabled_rules:
            continue
        # Prefer LanguageTool's own context snippet when available, otherwise fall back
        lt_ctx = (m.context or "").strip()
        ctx = lt_ctx or find_sentence_context(text, m.offset, m.errorLength)
        if any(phrase in ctx for phrase in ignored_phrases):
            continue
        issues.append({
            "type": "grammar",
            "rule": m.ruleId,
            "message": m.message,
            "suggestion": m.replacements[0] if m.replacements else "",
            "context": ctx,
            "category": getattr(m, "category", None),
            "issue_type": getattr(m, "ruleIssueType", None),
        })
    return issues

# ——— Main ———
def main():
    Path(REPORT.parent).mkdir(parents=True, exist_ok=True)
    chunk_dir = target_path / "chapters"
    if not chunk_dir.exists():
        print(f"❌ No chunks found in {chunk_dir}.")
        return

    tool = language_tool_python.LanguageTool('en-GB', remote_server=LT_URL)

    # Load suppressions (ignored phrases and disabled rule IDs)
    suppressions = load_suppressions([GLOBAL_SUPPRESSIONS, BOOK_SUPPRESSIONS])
    disabled_rules = {line for line in suppressions if line.isupper()}
    ignored_phrases = {line for line in suppressions if not line.isupper()}

    grammar_issues = []
    chunk_files = sorted(chunk_dir.glob("*.txt"))

    pbar = tqdm(
        chunk_files,
        desc="Scanning chapters",
        dynamic_ncols=True,
        file=sys.stdout,
    )

    for f in pbar:
        # Show current filename beside the bar.
        pbar.set_postfix_str(f.name)

        chunk = f.read_text(encoding="utf-8")
        # Apply suppressions: disables rules and ignores matches in specified contexts
        issues = check_grammar(chunk, tool, disabled_rules, ignored_phrases)
        for issue in issues:
            issue["chunk"] = f.name
        # Rule strength: base > _2 > _3
        rule_strength = {
            "COMMA_COMPOUND_SENTENCE": 0,
            "COMMA_COMPOUND_SENTENCE_2": 1,
            "COMMA_COMPOUND_SENTENCE_3": 2
        }

        # Store the best version of each context
        issue_map = {}

        for issue in issues:
            context = issue["context"]
            rule = issue["rule"]

            if rule.startswith("COMMA_COMPOUND_SENTENCE"):
                if context not in issue_map:
                    issue_map[context] = issue
                else:
                    existing = issue_map[context]
                    if rule_strength.get(rule, 99) < rule_strength.get(existing["rule"], 99):
                        issue_map[context] = issue
            else:
                grammar_issues.append(issue)

        # Add only the strongest COMMA_COMPOUND_SENTENCE-type issues
        grammar_issues.extend(issue_map.values())


    # ——— Write report, grouped by rule ———
    from collections import defaultdict

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(f"# ✏️ Grammar Report (Grouped by Rule)\n")
        f.write(f"_Generated: {ts}_\n\n")

        # Build rule summary table
        from collections import Counter
        rule_counts = Counter(err["rule"] for err in grammar_issues)

        f.write("## 📊 Rule Summary\n\n")
        f.write("| Rule ID | Occurrences |\n")
        f.write("|---------|-------------|\n")
        total = 0
        sorted_rules = sorted(rule_counts.items(), key=lambda item: item[1], reverse=True)
        for rule, count in sorted_rules:
            total += count
            f.write(f"| `{rule}` | {count} |\n")
        f.write(f"| **Total** | **{total}** |\n\n")



        # Group by rule, then by issue
        grouped = defaultdict(list)
        for err in grammar_issues:
            grouped[err["rule"]].append(err)

        for rule, group in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True):

            group = sorted(group, key=lambda e: (e["chunk"], e["context"]))
            message = group[0]["message"]
            category = group[0].get("category")
            issue_type = group[0].get("issue_type")

            f.write(f"## 🧩 Rule: `{rule}`\n")
            f.write(f"**Message:** {message}\n")
            if category:
                f.write(f"**Category:** {category}\n")
            if issue_type:
                f.write(f"**Issue type:** {issue_type}\n")
            f.write(f"Occurrences: {len(group)}\n\n")

            if rule == "COMMA_COMPOUND_SENTENCE":
                subgroups = {
                    "but": [],
                    "so": [],
                    "and": [],
                    "other": []
                }
                for err in group:
                    suggestion = err["suggestion"].lower()
                    if "but" in suggestion:
                        subgroups["but"].append(err)
                    elif "so" in suggestion:
                        subgroups["so"].append(err)
                    elif "and" in suggestion:
                        subgroups["and"].append(err)
                    else:
                        subgroups["other"].append(err)


                for label in ["but", "so", "and", "other"]:
                    if not subgroups[label]:
                        continue
                    f.write(f"### 🔸 Compound with `{label}` — {len(subgroups[label])} instance{'s' if len(subgroups[label]) != 1 else ''}\n\n")
                    for err in subgroups[label]:
                        f.write(f"#### 📍 Chunk: `{err['chunk']}`\n")
                        if err['suggestion']:
                            f.write(f"**Suggestion:** `{err['suggestion']}`\n")
                        f.write(f"**Context:**\n")
                        f.write("```text\n")
                        f.write(err["context"].strip() + "\n")
                        f.write("```\n\n")
            else:
                for err in group:
                    f.write(f"### 📍 Chunk: `{err['chunk']}`\n")
                    if err['suggestion']:
                        f.write(f"**Suggestion:** `{err['suggestion']}`\n")
                    f.write(f"**Context:**\n")
                    f.write("```text\n")
                    f.write(err["context"].strip() + "\n")
                    f.write("```\n\n")



    # Terminal summary: path first, then a concise final status line.
    total_issues = len(grammar_issues)
    unique_rules = len({e.get("rule") for e in grammar_issues if e.get("rule")})

    print(f"Report written to {REPORT}")

    if total_issues == 0:
        print("✅ No grammar/house-style issues detected")
    else:
        print(f"⚠️  Found {total_issues} issue(s) across {unique_rules} rule(s)")

if __name__ == "__main__":
    main()
