#!/usr/bin/env python3
"""
## Ward/Wards Audit — what this script does

This script scans the pre-chunked chapter files and produces a report of every
paragraph that contains *any* `-ward` or `-wards` form (forward/forwards,
toward/towards, etc.). Its purpose is to collect all **advisory or questionable**
usages for a style pass, with help from LanguageTool and an AI assistant.

It does a few main things:

1. **Starts and talks to LanguageTool**
   - Spins up a local `languagetool-server.jar` on port 8081 (and shuts it down
     cleanly on exit), using the same discovery logic as the other ETNA scripts.
   - Uses the `en-GB` ruleset via `language_tool_python`, pointed at that server.
   - For each paragraph, runs a LanguageTool check and keeps only matches from
     **advisory** ward-related rules (currently `ETNA_WARD_FORWARD_TO_FORWARDS`
     and any future rules you explicitly add to this script as advisory).

2. **Finds all paragraphs containing `-ward` / `-wards` forms**
   - Splits each chunk file in `chapters/` into paragraphs using one or more
     blank lines as separators.
   - Uses a regex to find all occurrences of:
     - forward(s), backward(s), upward(s), downward(s),
       inward(s), outward(s), onward(s), homeward(s),
       northward(s), southward(s), eastward(s), westward(s),
       afterward(s), seaward(s), landward(s), skyward(s),
       leftward(s), rightward(s), etc.
     - toward/towards are explicitly included.
   - Any paragraph with at least one such token *flagged by an advisory rule* is included in the report,
     ensuring that the report focuses on borderline or context-dependent usages rather than hard violations.

3. **Aligns LanguageTool matches with the tokens**
   - For each paragraph, the script lines up:
     - every `-ward/-wards` token found by regex, and
     - any LanguageTool matches whose offsets overlap that token,
       restricted to your ward rules (including future `ETNA_WARD_*`).
   - Each token is tagged as either:
     - **"flagged by LT"** with the rule ID and message, or
     - **"not flagged by LT"** (useful to spot coverage gaps in the rules).

4. **Writes a markdown report for AI-assisted review**
   - The report `r_08_ward_audit.md` starts with:
     - a short summary, and
     - a reusable instruction block aimed at AI models, explaining:
       - the BrE house style preference for `towards/forwards/afterwards`,
       - that most `ETNA_WARD_*` rules represent strong style preferences,
       - that `ETNA_WARD_FORWARD_TO_FORWARDS` is advisory and should be treated
         with extra caution, and
       - what the AI should do for each paragraph (decide whether to keep or
         change, and explain why).
   - Then, for each paragraph:
     - shows the chunk filename and paragraph index,
     - lists the `-ward/-wards` tokens and whether each is:
       - “flagged by LanguageTool (rule/message)” or
       - “not flagged by LanguageTool”,
     - and includes the full paragraph as a fenced code block.

The overall goal is to:
- give you *filtered* visibility of **borderline or advisory** `-ward/-wards` usage,
- keep “hard” style violations (e.g. `toward` → `towards`, bare `-ward` adverbs, wrong `-wards` before nouns) in a separate, more mechanical report,
- provide a clean, copy-paste-ready report that you can feed to AI to decide
  which advisory cases should actually be changed or left as-is,
- and highlight patterns where you might want to tighten or relax your advisory rules.
"""

import argparse
from pathlib import Path
from datetime import datetime
import re
import subprocess
import atexit
import time
import os
import sys

import language_tool_python
from tqdm import tqdm

# ——— CLI Setup ———
parser = argparse.ArgumentParser(description="Audit -ward/-wards usage in pre-chunked text files.")
parser.add_argument("path", nargs="?", default=".", help="Path to folder (default: current directory)")
args = parser.parse_args()

target_path = Path(args.path)
print(f"🔍 Running ward/wards audit on: {target_path.resolve()}")

REPORT = target_path / "reports/r_08_ward_audit.md"

# ---------------- Config (optional, used for shared paths) ----------------
REPO_ROOT = Path(__file__).resolve().parents[2]

# Use the shared loader (config.yaml overlaid by optional config.local.yaml).
# Config is optional for this script: if it cannot be loaded, PATHS stays empty
# and the JAR must be provided via the environment variable.
sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
try:
    from common import load_paths  # noqa: E402
    PATHS = load_paths(require_base=False)
except Exception:
    PATHS = {}


# ——— Ward/Wards Patterns & LanguageTool Rules ———

WARD_WORDS = r"""
    toward|towards|
    forward|forwards|
    backward|backwards|
    upward|upwards|
    downward|downwards|
    inward|inwards|
    outward|outwards|
    onward|onwards|
    homeward|homewards|
    afterward|afterwards|
    seaward|seawards|
    landward|landwards|
    skyward|skywards|
    northward|northwards|
    southward|southwards|
    eastward|eastwards|
    westward|westwards|
    leftward|leftwards|
    rightward|rightwards
"""

WARD_REGEX = re.compile(rf"\b(?:{WARD_WORDS})\b", re.IGNORECASE | re.VERBOSE)

# Advisory ward-related rules for this report.
# Hard “must-change” rules (e.g. ETNA_TOWARD_TO_TOWARDS, ETNA_WARD_ADD_S,
# ETNA_WARDS_TO_WARD_ADJECTIVAL) are handled by a separate report.
WARD_RULE_IDS = {
    "ETNA_WARD_FORWARD_TO_FORWARDS",
}


# ——— Start LanguageTool Server ———
LT_PORT = int(os.environ.get("ETNA_LANGUAGETOOL_PORT", "8081"))
LT_URL = f"http://localhost:{LT_PORT}"

lt_process = None  # process handle

def resolve_languagetool_jar() -> Path:
    """Resolve the LanguageTool server JAR.

    Priority:
      1) env var ETNA_LANGUAGETOOL_SERVER_JAR
      2) scripts/common/config.yaml (+ optional config.local.yaml) keys (a → b → c):
         - paths.languagetool_server_jar_a
         - paths.languagetool_server_jar_b
         - paths.languagetool_server_jar_c

    Config paths may be absolute or repo-relative.
    """
    checked: list[Path] = []

    env = os.environ.get("ETNA_LANGUAGETOOL_SERVER_JAR")
    if env:
        p = Path(env).expanduser()
        checked.append(p)
        if p.exists():
            return p

    # Try up to three configured jar locations (a, b, c)
    for key in ("languagetool_server_jar_a", "languagetool_server_jar_b", "languagetool_server_jar_c"):
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
    print("  1) Set env var ETNA_LANGUAGETOOL_SERVER_JAR to the full path of languagetool-server.jar")
    print("  2) Or add to etna/scripts/common/config.yaml (one or more):")
    print("     paths:")
    print("       languagetool_server_jar_a: \"/full/path/to/languagetool-server.jar\"")
    print("       languagetool_server_jar_b: \"/another/path/to/languagetool-server.jar\"")
    print("       languagetool_server_jar_c: \"path/to/languagetool-server.jar\"  # repo-relative ok")
    sys.exit(1)

def start_languagetool_server():
    """
    Starts the LanguageTool HTTP server on localhost:8081 and waits until ready.
    """
    global lt_process
    jar_path = resolve_languagetool_jar()

    lt_root = jar_path.parent  # directory that contains the JAR and org/languagetool/...
    classpath = f"{lt_root}:{jar_path}"  # on macOS/Linux, ':' separates entries

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
    if lt_process:
        try:
            _, err = lt_process.communicate(timeout=2)
            print("🧾 Server output:\n", err)
        except Exception:
            print("🧾 Server output could not be read (timeout or other error).")
        finally:
            lt_process.terminate()
    exit(1)


def stop_languagetool_server():
    global lt_process
    if lt_process:
        print("🛑 Stopping LanguageTool server...")
        lt_process.terminate()


atexit.register(stop_languagetool_server)
start_languagetool_server()


# ——— Helpers ———

def split_paragraphs(text: str):
    """
    Split text into paragraphs using one or more blank lines as separators.
    Returns a list of non-empty, stripped paragraph strings.
    """
    paras = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paras if p.strip()]


def get_ward_matches_from_lt(paragraph: str, tool):
    """
    Run LanguageTool over a paragraph and return only matches from ward-related
    rules: anything in WARD_RULE_IDS or with a rule ID starting 'ETNA_WARD'.
    """
    matches = tool.check(paragraph)
    ward_matches = []
    for m in matches:
        rule_id = m.ruleId
        # Only include advisory rules explicitly listed in WARD_RULE_IDS.
        if rule_id in WARD_RULE_IDS:
            ward_matches.append(m)
    return ward_matches


def align_tokens_with_lt(paragraph: str, lt_matches):
    """
    For a given paragraph and its LT matches, return a list of token records:

    Each record:
        {
            "token": original word as it appears,
            "offset": character offset in the paragraph,
            "flagged_by_lt": bool,
            "lt_rules": [ruleId, ...],
            "lt_messages": [message, ...],
            "primary_suggestion": optional single-string replacement from LT,
        }
    """
    tokens = []
    for match in WARD_REGEX.finditer(paragraph):
        token_text = match.group(0)
        start = match.start()
        end = match.end()

        flagged_rules = []
        flagged_messages = []
        primary_suggestion = None

        for m in lt_matches:
            m_start = m.offset
            m_end = m.offset + m.errorLength
            # Consider the token covered if ranges overlap
            if not (m_end <= start or m_start >= end):
                flagged_rules.append(m.ruleId)
                flagged_messages.append(m.message)
                if primary_suggestion is None and getattr(m, "replacements", None):
                    if m.replacements:
                        primary_suggestion = m.replacements[0]

        tokens.append({
            "token": token_text,
            "offset": start,
            "flagged_by_lt": bool(flagged_rules),
            "lt_rules": flagged_rules,
            "lt_messages": flagged_messages,
            "primary_suggestion": primary_suggestion,
        })

    return tokens


# ——— Main ———

def main():
    Path(REPORT.parent).mkdir(parents=True, exist_ok=True)
    chunk_dir = target_path / "chapters"
    if not chunk_dir.exists():
        print(f"❌ No chunks found in {chunk_dir}.")
        return

    tool = language_tool_python.LanguageTool('en-GB', remote_server=LT_URL)

    results = []
    chunk_files = sorted(chunk_dir.glob("*.txt"))

    pbar = tqdm(
        chunk_files,
        desc="Scanning chapters",
        dynamic_ncols=True,
        file=sys.stdout,
    )

    for f in pbar:
        pbar.set_postfix_str(f.name)
        chunk = f.read_text(encoding="utf-8")
        paragraphs = split_paragraphs(chunk)

        for p_idx, para in enumerate(paragraphs, start=1):
            # Quick regex check: skip paragraphs with no -ward/-wards forms at all
            if not WARD_REGEX.search(para):
                continue

            lt_matches = get_ward_matches_from_lt(para, tool)
            tokens = align_tokens_with_lt(para, lt_matches)

            # Only keep paragraphs where at least one -ward/-wards token
            # is flagged by LanguageTool as questionable
            if not any(t["flagged_by_lt"] for t in tokens):
                continue

            results.append({
                "chunk": f.name,
                "paragraph_index": p_idx,
                "paragraph": para,
                "tokens": tokens,
            })

    # ——— Write report ———
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_paragraphs = len(results)

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("# 🎯 Ward/Wards Style Audit\n\n")
        f.write(f"_Generated: {ts}_  \n")
        f.write(f"_Source path: `{target_path}`_\n\n")

        # Instructions for AI
        f.write("## 🧠 Instructions for AI Reviewer\n\n")
        f.write(
            "You are reviewing `-ward` / `-wards` usage in a British English fiction manuscript.\n"
            "This report only includes paragraphs where at least one -ward/-wards token has been flagged by an **advisory** rule (currently ETNA_WARD_FORWARD_TO_FORWARDS).\n"
            "Hard style violations like `toward`→`towards`, bare `-ward` adverbs, or `-wards` before nouns are handled in a different report and should be assumed already fixed.\n\n"
        )
        f.write("Please follow these house-style principles:\n\n")
        f.write("1. **General preference for -wards adverbs**  \n")
        f.write("   - Prefer forms like **towards, backwards, forwards, upwards, onwards, afterwards**\n")
        f.write("     when they function as *directional adverbs of motion* (e.g. “He walked forwards into the room”).\n")
        f.write("   - American forms like **toward** should normally become **towards**.\n\n")
        f.write("2. **Context where -ward is fine or preferred**  \n")
        f.write("   - Fixed expressions or set phrases (e.g. “forward planning”, “backward compatibility”) may keep **-ward**.\n")
        f.write("   - Uses functioning more like adjectives than adverbs of motion can reasonably be **-ward**.\n\n")
        f.write("3. **Special handling for “forward” vs “forwards”**  \n")
        f.write("   - Treat recommendations from the `ETNA_WARD_FORWARD_TO_FORWARDS` rule as **advisory**, not absolute.\n")
        f.write("   - **forward** is often acceptable (and idiomatic) in figurative or static uses\n")
        f.write("     (e.g. “looking forward”, “a step forward in his career”).\n")
        f.write("   - Use **forwards** especially for clear, literal motion through space.\n\n")
        f.write("4. **What to do for each paragraph (advisory cases only)**  \n")
        f.write("   For every paragraph below:\n")
        f.write("   - Look at the list of tokens and how LanguageTool has flagged them under advisory rules.\n")
        f.write("   - Decide for each token whether it is **fine as is** within BrE with a preference for -wards, or whether you would **recommend a change**.\n")
        f.write("   - If you recommend a change, suggest the exact replacement and **explain briefly why**\n")
        f.write("     (e.g. “BrE style prefers ‘towards’ here for physical movement”, or\n")
        f.write("     “adjectival use in a fixed phrase, so ‘forward’ is appropriate”).\n")
        f.write("   - If LanguageTool did **not** flag a token but you think it clashes with the style guide,\n")
        f.write("     call that out explicitly; this may indicate the need for a new or refined `ETNA_WARD_*` rule.\n\n")
        f.write("5. **Output format suggestion**  \n")
        f.write("   For each paragraph, a useful structure would be:\n\n")
        f.write("   - Bullet list per token:\n")
        f.write("     - `<token>` – **keep** / **change to <X>** (reason…)\n")
        f.write("   - Only elaborate where the choice is non-obvious or stylistically important.\n\n")

        # Summary
        f.write("## 📊 Summary\n\n")
        f.write(f"- Total paragraphs with at least one `-ward/-wards` token flagged by an advisory rule: **{total_paragraphs}**\n\n")

        # Group paragraphs by chunk
        from collections import defaultdict
        grouped = defaultdict(list)
        for entry in results:
            grouped[entry["chunk"]].append(entry)

        for chunk_name in sorted(grouped.keys()):
            f.write(f"## 📁 Chunk: `{chunk_name}`\n\n")
            for entry in grouped[chunk_name]:
                p_idx = entry["paragraph_index"]
                para = entry["paragraph"]
                tokens = entry["tokens"]

                f.write(f"### 🔎 Paragraph {p_idx}\n\n")

                flagged_tokens = [t for t in tokens if t["flagged_by_lt"]]
                unflagged_tokens = [t for t in tokens if not t["flagged_by_lt"]]

                f.write("**Flagged -ward/-wards tokens (LanguageTool thinks these may conflict with the house style):**\n\n")
                if not flagged_tokens:
                    f.write("- _(No tokens flagged — this paragraph is only here because it shares a chunk with other flagged text.)_\n\n")
                else:
                    for t in flagged_tokens:
                        token = t["token"]
                        rules = ", ".join(t["lt_rules"])
                        msgs = " | ".join(t["lt_messages"])
                        suggestion = t.get("primary_suggestion")
                        if suggestion:
                            f.write(f"- `{token}` → `{suggestion}` — `{rules}`: {msgs}\n")
                        else:
                            f.write(f"- `{token}` — `{rules}`: {msgs}\n")
                    f.write("\n")

                if unflagged_tokens:
                    f.write("**Other -ward/-wards tokens in this paragraph (not flagged by LT, but you may still want to check):**\n\n")
                    for t in unflagged_tokens:
                        token = t["token"]
                        f.write(f"- `{token}`\n")
                    f.write("\n")

                f.write("**Paragraph text:**\n\n")
                f.write("```text\n")
                f.write(para + "\n")
                f.write("```\n\n")

    # Terminal summary: path first, then a concise final status line.
    total_paras = len(results)
    flagged_tokens = sum(
        1
        for entry in results
        for t in entry.get("tokens", [])
        if t.get("flagged_by_lt")
    )
    unique_rules = {
        r
        for entry in results
        for t in entry.get("tokens", [])
        for r in t.get("lt_rules", [])
        if r
    }

    print(f"Report written to {REPORT}")

    if total_paras == 0:
        print("✅ No advisory -ward/-wards cases detected")
    else:
        print(
            f"⚠️  Found {total_paras} paragraph(s) flagged ({flagged_tokens} token(s), {len(unique_rules)} rule(s))"
        )


if __name__ == "__main__":
    main()