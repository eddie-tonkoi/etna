

#!/usr/bin/env python3
"""02_continuity_check.py

Continuity Ledger (MVP)

Goal:
- Extract explicit continuity facts from each chapter (LLM -> JSON objects with evidence quotes)
- Build a JSONL "fact table"
- Deterministically flag contradictions across chapters (same entity + fact_type, different value)

Designed to be "suggest-only": it never edits text.

Usage:
  python 02_continuity_check.py /path/to/book_workdir

Outputs:
  /path/to/book_workdir/reports/r_lm_02_continuity_facts.jsonl
  /path/to/book_workdir/reports/r_lm_02_continuity_report.md

Assumes a local llama.cpp `llama-server` OpenAI-compatible endpoint:
  http://127.0.0.1:8080/v1/chat/completions

Environment overrides (optional):
  LLAMA_API_BASE   (default: http://127.0.0.1:8080)
  LLAMA_MODEL      (default: "")  # often not required for llama-server
  LLAMA_TEMPERATURE (default: 0)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# -----------------------------
# Configuration
# -----------------------------

API_BASE = os.environ.get("LLAMA_API_BASE", "http://127.0.0.1:8080").rstrip("/")
CHAT_URL = f"{API_BASE}/v1/chat/completions"
MODEL = os.environ.get("LLAMA_MODEL", "")
TEMPERATURE = float(os.environ.get("LLAMA_TEMPERATURE", "0"))

# Keep extraction conservative so the ledger stays readable.
MAX_FACTS_PER_CHAPTER = 60
MAX_EVIDENCE_WORDS = 20

# Input safety: if a chapter is enormous, truncate to avoid blowing context.
# (llama-server can handle large context, but this keeps the MVP robust.)
MAX_CHAPTER_CHARS = 60_000

# Many llama-server instances run with a small context (e.g. 4096). To avoid 400 errors,
# we extract facts from smaller excerpts and then merge/deduplicate.
MAX_EXCERPT_CHARS = 9_000

# Contradiction heuristics
MIN_DISTINCT_VALUES_TO_FLAG = 2

FACT_TYPES = [
    "eye_colour",
    "hair_colour",
    "height_build",
    "scars_marks",
    "injury_condition",
    "relationship",
    "nickname_alias",
    "location",
    "object_state",
    "time_anchor",
]

# Only these fact types are treated as "should be consistent" for contradiction checks.
# Others (location/time_anchor/object_state/relationship) are often multi-valued within a chapter.
SINGLE_VALUED_FACT_TYPES = {
    "eye_colour",
    "hair_colour",
    "height_build",
    "scars_marks",
    "injury_condition",
    "nickname_alias",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


# -----------------------------
# Helpers
# -----------------------------


def _flush_print(msg: str) -> None:
    print(msg, flush=True)


def iter_chapter_files(base_path: Path) -> List[Path]:
    """Find chapter .txt files.

    Preference order:
      1) base_path/chapters/*.txt
      2) base_path/*.txt

    Sorts by filename.
    """
    candidates: List[Path] = []
    chapters_dir = base_path / "chapters"
    if chapters_dir.exists() and chapters_dir.is_dir():
        candidates = sorted([p for p in chapters_dir.glob("*.txt") if p.is_file()])
    if not candidates:
        candidates = sorted([p for p in base_path.glob("*.txt") if p.is_file()])
    # Prefer files that look like chapters
    chapterish = [p for p in candidates if re.search(r"chapter|ch_\d+|_\d+", p.name, re.IGNORECASE)]
    return chapterish if chapterish else candidates


def split_sentences(text: str) -> List[str]:
    """Very simple sentence-ish splitting for evidence context."""
    parts = [p.strip() for p in SENT_SPLIT_RE.split(text) if p.strip()]
    # Avoid absurdly long "sentences"
    out = []
    for p in parts:
        if len(p) > 800:
            out.append(p[:800] + " …")
        else:
            out.append(p)
    return out


def chunk_text(text: str, max_chars: int) -> List[str]:
    """Chunk text into excerpts <= max_chars, preferring paragraph boundaries."""
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append("\n\n".join(buf))
            buf = []
            size = 0

    for p in paras:
        # If a single paragraph is too large, hard-split it.
        if len(p) > max_chars:
            flush()
            for i in range(0, len(p), max_chars):
                part = p[i : i + max_chars]
                if part.strip():
                    chunks.append(part.strip())
            continue

        if size + len(p) + (2 if buf else 0) > max_chars:
            flush()
        buf.append(p)
        size += len(p) + (2 if buf else 0)

    flush()
    return chunks


def normalise_value(v: str) -> str:
    v = v.strip().lower()
    v = re.sub(r"\s+", " ", v)
    # remove trivial punctuation
    v = v.strip(" .,:;!?")
    return v


def evidence_is_in_text(evidence: str, text: str) -> bool:
    """Strict-ish evidence check: must appear as a substring (after light normalisation).

    This prevents hallucinated facts from entering the ledger.
    """
    if not evidence:
        return False
    ev = " ".join(evidence.strip().split())
    if len(ev) < 6:
        return False
    # Direct substring
    if ev in text:
        return True
    # Relaxed: normalise curly quotes/apostrophes
    t2 = text.replace("’", "'")
    ev2 = ev.replace("’", "'")
    return ev2 in t2


def post_json(url: str, payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as e:
        raise RuntimeError(f"HTTPError {e.code}: {e.read().decode('utf-8', errors='replace')}")
    except URLError as e:
        raise RuntimeError(f"URLError: {e}")


def call_llama_chat(messages: List[Dict[str, str]], max_tokens: int = 1400) -> str:
    payload: Dict[str, Any] = {
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if MODEL:
        payload["model"] = MODEL

    out = post_json(CHAT_URL, payload)
    try:
        return out["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Unexpected llama-server response shape: {out}")


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    """Extract a JSON array from model output.

    The prompt asks for strict JSON. This is a robust fallback if the model adds prose.
    """
    text = text.strip()
    # If it is valid JSON already
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except Exception:
        pass

    # Fallback: find first [ ... ] block
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    blob = m.group(0)
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except Exception:
        return []
    return []


def build_prompt(
    chapter_name: str, heading: str, body: str, excerpt_idx: int, excerpt_total: int
) -> List[Dict[str, str]]:
    system = (
        "You are extracting a continuity ledger from a fiction chapter. "
        "Only extract facts that are explicitly stated in the text. "
        "Do NOT infer or guess. "
        "Every fact MUST include an evidence quote copied verbatim from the chapter. "
        "Extract facts ONLY from the provided excerpt (do not use outside knowledge). "
        f"Limit evidence quotes to at most {MAX_EVIDENCE_WORDS} words. "
        f"Return a JSON array only (no prose). "
        f"Each item must have keys: entity, fact_type, value, evidence. "
        f"fact_type must be one of: {', '.join(FACT_TYPES)}. "
        f"Return at most {MAX_FACTS_PER_CHAPTER} items. "
        "If you are unsure, omit the fact."
    )

    user = (
        f"Chapter file: {chapter_name}\n"
        f"Excerpt: {excerpt_idx}/{excerpt_total}\n"
        f"Chapter heading (if present): {heading}\n\n"
        "CHAPTER TEXT START\n"
        f"{body}\n"
        "CHAPTER TEXT END\n\n"
        "Return the JSON array now."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


@dataclass
class FactRow:
    file: str
    heading: str
    entity: str
    fact_type: str
    value: str
    evidence: str

    original_value: str
    original_entity: str


def validate_and_normalise(rows: List[Dict[str, Any]], file_name: str, heading: str, full_text: str) -> List[FactRow]:
    out: List[FactRow] = []
    for r in rows:
        try:
            ent = str(r.get("entity", "")).strip()
            ftype = str(r.get("fact_type", "")).strip()
            val = str(r.get("value", "")).strip()
            ev = str(r.get("evidence", "")).strip()
        except Exception:
            continue

        if not ent or not ftype or not val or not ev:
            continue

        ftype_l = ftype.lower()
        if ftype_l not in FACT_TYPES:
            continue

        # Evidence must appear in chapter text
        if not evidence_is_in_text(ev, full_text):
            continue

        # Keep evidence short-ish
        if len(ev.split()) > MAX_EVIDENCE_WORDS:
            ev = " ".join(ev.split()[:MAX_EVIDENCE_WORDS]) + " …"

        out.append(
            FactRow(
                file=file_name,
                heading=heading,
                entity=ent,
                fact_type=ftype_l,
                value=normalise_value(val),
                evidence=ev,
                original_value=val,
                original_entity=ent,
            )
        )

    return out


def find_contradictions(facts: List[FactRow]) -> Dict[Tuple[str, str], List[FactRow]]:
    """Flag likely continuity contradictions across chapters.

    Rules (MVP):
    - Only consider SINGLE_VALUED_FACT_TYPES (e.g., eye_colour).
    - Only flag when conflicting values are evidenced in >= 2 different files.
      (Avoids noisy intra-chapter multi-mentions.)
    """
    buckets: Dict[Tuple[str, str], List[FactRow]] = {}
    for f in facts:
        if f.fact_type not in SINGLE_VALUED_FACT_TYPES:
            continue
        key = (f.entity.strip().lower(), f.fact_type)
        buckets.setdefault(key, []).append(f)

    contradictions: Dict[Tuple[str, str], List[FactRow]] = {}
    for key, items in buckets.items():
        vals = {it.value for it in items}
        if len(vals) < MIN_DISTINCT_VALUES_TO_FLAG:
            continue
        files = {it.file for it in items}
        if len(files) < 2:
            continue
        contradictions[key] = items
    return contradictions


def write_jsonl(path: Path, rows: Iterable[FactRow], ts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(
                json.dumps(
                    {
                        "generated": ts,
                        "file": r.file,
                        "heading": r.heading,
                        "entity": r.original_entity,
                        "fact_type": r.fact_type,
                        "value": r.original_value,
                        "value_norm": r.value,
                        "evidence": r.evidence,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def write_report(path: Path, ts: str, facts: List[FactRow], contradictions: Dict[Tuple[str, str], List[FactRow]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write("# 🧠 Continuity Ledger Report\n")
        f.write(f"_Generated: {ts}_\n\n")
        f.write(f"LLM endpoint: `{CHAT_URL}`\n\n")
        f.write(f"Facts extracted: **{len(facts)}**\n\n")
        f.write(f"Potential contradictions (cross-chapter, single-valued types): **{len(contradictions)}**\n\n")

        if not contradictions:
            f.write("✅ No cross-chapter contradictions detected (based on explicit extracted facts).\n")
            return

        # Sort keys for stable output
        keys = sorted(contradictions.keys(), key=lambda k: (k[0], k[1]))
        for idx, key in enumerate(keys, 1):
            ent, ftype = key
            items = contradictions[key]

            f.write(f"## {idx}. {ent} — {ftype}\n\n")

            # Group by normalised value
            by_val: Dict[str, List[FactRow]] = {}
            for it in items:
                by_val.setdefault(it.value, []).append(it)

            # Show each distinct value with evidence
            for v_i, (val_norm, group) in enumerate(sorted(by_val.items(), key=lambda kv: kv[0]), 1):
                # Preserve one original value for display
                display_val = group[0].original_value
                f.write(f"### Value {v_i}: {display_val}\n\n")
                for g in group:
                    f.write(f"- **{g.file}** ({g.heading}) — “{g.evidence}”\n")
                f.write("\n")


def main() -> None:
    # Make progress output appear promptly even under wrappers.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    t0 = time.perf_counter()

    if len(sys.argv) < 2:
        print("❌ Please provide the working directory path as an argument.")
        sys.exit(1)

    base_path = Path(sys.argv[1])
    if not base_path.is_dir():
        print(f"❌ Provided path is not a directory: {base_path}")
        sys.exit(1)

    report_dir = base_path / "reports"
    facts_path = report_dir / "r_lm_02_continuity_facts.jsonl"
    report_path = report_dir / "r_lm_02_continuity_report.md"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _flush_print("[cont] Stage: discover chapters")
    files = iter_chapter_files(base_path)
    if not files:
        print("❌ No .txt files found to scan.")
        sys.exit(1)
    _flush_print(f"[cont] Found {len(files)} file(s)")

    # Quick connectivity check early
    _flush_print("[cont] Stage: LLM connectivity check")
    try:
        _ = call_llama_chat([
            {"role": "system", "content": "Respond with: OK"},
            {"role": "user", "content": "Say OK"},
        ], max_tokens=16)
    except Exception as e:
        print(f"❌ Could not reach llama-server at {CHAT_URL}: {e}")
        sys.exit(1)

    all_facts: List[FactRow] = []

    _flush_print("[cont] Stage: extract facts per chapter")
    for i, fp in enumerate(files, 1):
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        heading = ""
        body = raw

        # If your chapter files begin with a heading line then blank line, mirror your other scripts.
        lines = raw.splitlines()
        if lines:
            heading = lines[0].strip()
        if len(lines) > 2 and lines[1].strip() == "":
            body = "\n".join(lines[2:])

        if len(body) > MAX_CHAPTER_CHARS:
            _flush_print(f"[cont] {fp.name}: truncating {len(body):,} chars -> {MAX_CHAPTER_CHARS:,} chars")
            body = body[:MAX_CHAPTER_CHARS]

        excerpts = chunk_text(body, MAX_EXCERPT_CHARS)
        _flush_print(f"[cont] ({i}/{len(files)}) Extracting: {fp.name} ({len(body):,} chars, {len(excerpts)} excerpt(s))")

        kept_for_chapter: List[FactRow] = []
        for ex_idx, ex in enumerate(excerpts, 1):
            _flush_print(f"[cont]   - excerpt {ex_idx}/{len(excerpts)} ({len(ex):,} chars)")
            messages = build_prompt(fp.name, heading, ex, ex_idx, len(excerpts))
            try:
                content = call_llama_chat(messages, max_tokens=1200)
            except Exception as e:
                _flush_print(f"[cont] ⚠️  LLM call failed for {fp.name} excerpt {ex_idx}: {e}")
                continue

            rows = extract_json_array(content)
            facts = validate_and_normalise(rows, fp.name, heading, raw)
            kept_for_chapter.extend(facts)

            # Respect per-chapter cap early
            if len(kept_for_chapter) >= MAX_FACTS_PER_CHAPTER:
                kept_for_chapter = kept_for_chapter[:MAX_FACTS_PER_CHAPTER]
                break

        # Deduplicate within the chapter by (entity, fact_type, value_norm, evidence)
        seen = set()
        deduped: List[FactRow] = []
        for fr in kept_for_chapter:
            k = (fr.entity.strip().lower(), fr.fact_type, fr.value, fr.evidence)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(fr)

        _flush_print(f"[cont] {fp.name}: kept {len(deduped)} fact(s)")
        all_facts.extend(deduped)

    _flush_print("[cont] Stage: analyse contradictions")
    contradictions = find_contradictions(all_facts)

    _flush_print("[cont] Stage: write outputs")
    write_jsonl(facts_path, all_facts, ts)
    write_report(report_path, ts, all_facts, contradictions)

    elapsed = time.perf_counter() - t0
    _flush_print(f"Report written to {report_path.resolve()}")
    _flush_print(f"Facts written to {facts_path.resolve()}")
    _flush_print(
        f"✅ Scanned {len(files)} file(s); extracted {len(all_facts)} fact(s); "
        f"flagged {len(contradictions)} potential contradiction set(s) in {elapsed:.1f} seconds"
    )


if __name__ == "__main__":
    main()