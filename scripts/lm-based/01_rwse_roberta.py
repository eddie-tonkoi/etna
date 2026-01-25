#!/usr/bin/env python3
import re
import sys
import json
from pathlib import Path
from datetime import datetime
import time
from typing import Iterable, List, Dict, Tuple, Optional

import math
import torch
import torch.nn.functional as F
from transformers import AutoTokenizer, AutoModelForMaskedLM
from rapidfuzz.distance import Levenshtein

# Optional: wordfreq for gating dynamic candidates (install with pip if desired)
try:
    from wordfreq import zipf_frequency
except Exception:
    zipf_frequency = None  # type: ignore

# -----------------------------
# Configuration
# -----------------------------

# Point to your local RoBERTa folder (downloaded earlier)
ROBERTA_PATH = "/Volumes/Projects/Developer/hf_models/roberta-large"

#
# Scoring threshold: flag only when the best candidate beats the original by >= this margin
# (log-prob space; tune after you see real chapter output).
MIN_MARGIN = 0.30

MIN_WORD_LEN = 4
MAX_WORD_LEN = 24
MAX_CANDIDATES = 25

# Dynamic edit-distance candidates are the main source of false positives for common words.
# If wordfreq is installed, only generate dynamic candidates for words with Zipf frequency <= this value.
# (Lower = rarer. 3.5 is a good starting point for fiction; tune 3.0–4.0.)
ZIPF_DYNAMIC_MAX = 3.5

# Bootstrap sources (all optional):
# - LanguageTool confusion sets
# - Homophones groups
# - A general wordlist (used for edit-distance neighbours)
DEFAULT_LT_CONFUSIONS = "/Applications/LanguageTool.app/Contents/Resources/en/confusion_sets.txt"
DEFAULT_HOMOPHONES = ""  # set to a local CSV/TXT if you have one
DEFAULT_WORDLISTS = ["/usr/share/dict/words", "/usr/dict/words"]

# Minimal seed confusions so you can validate the pipeline immediately.
SEED_CONFUSIONS: Dict[str, List[str]] = {
    "leach": ["leash"],
    "peek": ["peak", "pique"],
    "rein": ["reign"],
    "bare": ["bear"],
    "principle": ["principal"],
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

# Common function words produce lots of noisy "corrections" via edit-distance neighbours.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "because",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "as", "into",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "not", "no", "yes", "this", "that", "these", "those", "here", "there",
    "he", "she", "it", "they", "we", "i", "you", "me", "him", "her", "them",
    "my", "your", "his", "hers", "their", "our", "mine", "yours", "ours",
    "just", "still", "now", "one", "two", "all", "any", "some", "what", "when", "where",
}

def is_possessive_s(token: str) -> bool:
    tl = token.lower()
    return tl.endswith("'s") or tl.endswith("’s")


def allow_dynamic_for(word_lower: str) -> bool:
    """Return True if we should generate edit-distance candidates for this word."""
    if word_lower in STOPWORDS or len(word_lower) < MIN_WORD_LEN:
        return False
    if zipf_frequency is None:
        # If wordfreq isn't installed, fall back to the previous behaviour.
        return True
    try:
        return zipf_frequency(word_lower, "en") <= ZIPF_DYNAMIC_MAX
    except Exception:
        return True

def load_lt_confusions(path: str) -> Dict[str, List[str]]:
    """Parse LanguageTool-like confusion sets. Tolerant to multiple formats."""
    p = Path(path)
    if not path or not p.exists():
        return {}
    out: Dict[str, set] = {}
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept formats like: "a b" / "a\tb" / "a -> b" / "a,b"
        line = line.replace("->", " ")
        parts = re.split(r"[\t, ]+", line)
        parts = [x for x in parts if x]
        if len(parts) < 2:
            continue
        head = parts[0].lower()
        alts = [p.lower() for p in parts[1:] if p]
        if head not in out:
            out[head] = set()
        out[head].update(alts)
        # Make it bidirectional (useful for RWSE checks)
        for a in alts:
            out.setdefault(a, set()).add(head)
            for b in alts:
                if a != b:
                    out[a].add(b)
    return {k: sorted(v) for k, v in out.items()}

def load_homophones_groups(path: str) -> Dict[str, List[str]]:
    """Load homophones as comma/space separated groups; adds bidirectional pairs."""
    p = Path(path)
    if not path or not p.exists():
        return {}
    out: Dict[str, set] = {}
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\t, ]+", line)
        parts = [x.lower() for x in parts if x]
        if len(parts) < 2:
            continue
        for w in parts:
            out.setdefault(w, set()).update([x for x in parts if x != w])
    return {k: sorted(v) for k, v in out.items()}

def load_wordlist(paths: List[str]) -> set:
    vocab: set = set()
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            w = line.strip().lower()
            if not w or not w.isalpha():
                continue
            if MIN_WORD_LEN <= len(w) <= MAX_WORD_LEN:
                vocab.add(w)
    return vocab

def collect_book_vocab(base_path: Path) -> set:
    """Collect unique words from the book itself (chapters + preamble)."""
    vocab: set = set()
    for fp in iter_chapter_files(base_path):
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for m in WORD_RE.finditer(text):
            w = m.group(0).lower()
            if MIN_WORD_LEN <= len(w) <= MAX_WORD_LEN and w.replace("'", "").isalpha():
                vocab.add(w)
    return vocab

# -----------------------------
# SymSpell-style delete index for fast edit-distance candidate retrieval
# -----------------------------

def deletes(word: str, max_dist: int = 2) -> set:
    """Generate all deletes up to max_dist."""
    out = set()
    queue = {word}
    for _ in range(max_dist):
        new_queue = set()
        for w in queue:
            if len(w) <= 1:
                continue
            for i in range(len(w)):
                d = w[:i] + w[i+1:]
                if d not in out:
                    out.add(d)
                    new_queue.add(d)
        queue = new_queue
    return out

def build_delete_index(vocab: Iterable[str], max_dist: int = 2) -> Dict[str, List[str]]:
    idx: Dict[str, List[str]] = {}
    for w in vocab:
        for d in deletes(w, max_dist=max_dist):
            idx.setdefault(d, []).append(w)
    return idx

def edit_distance_candidates(word: str, idx: Dict[str, List[str]], max_dist: int = 2) -> List[str]:
    w = word.lower()
    cands: set = set()
    for d in deletes(w, max_dist=max_dist):
        for cand in idx.get(d, []):
            if cand == w:
                continue
            # Final exact filter
            if abs(len(cand) - len(w)) <= 2 and Levenshtein.distance(cand, w) <= max_dist:
                cands.add(cand)
    return sorted(cands)

def merge_maps(*maps: Dict[str, List[str]]) -> Dict[str, List[str]]:
    out: Dict[str, set] = {}
    for mp in maps:
        for k, vs in mp.items():
            out.setdefault(k, set()).update([v for v in vs if v and v != k])
    return {k: sorted(v) for k, v in out.items()}

def iter_chapter_files(base_path: Path) -> List[Path]:
    chapters_dir = base_path / "chapters"
    files = []
    pre = chapters_dir / "preamble.txt"
    if pre.exists():
        files.append(pre)
    files.extend(sorted(chapters_dir.glob("chapter_*.txt")))
    return files

def split_sentences(text: str) -> List[str]:
    # Simple, robust sentence splitter (good enough for a first pass).
    # Keeps punctuation.
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]

def token_spans(sentence: str) -> List[Tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(sentence)]

def close_candidate(a: str, b: str) -> bool:
    if a == b:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    return Levenshtein.distance(a, b) <= 2

def build_candidate_list(
    word: str,
    confusions: Dict[str, List[str]],
    homophones: Dict[str, List[str]],
    del_index: Dict[str, List[str]],
) -> Tuple[List[str], Dict[str, str]]:
    """Return (candidates, source_map) where source is 'bootstrap' or 'dynamic'."""
    w = word.lower()
    source: Dict[str, str] = {}
    cands: set = set()

    # Bootstrap candidates (higher trust)
    for cand in confusions.get(w, []):
        cl = cand.lower()
        if cl and cl != w:
            cands.add(cl)
            source[cl] = "bootstrap"
    for cand in homophones.get(w, []):
        cl = cand.lower()
        if cl and cl != w:
            cands.add(cl)
            source.setdefault(cl, "bootstrap")

    # Dynamic: edit-distance neighbours (lower trust; gated for common words)
    if allow_dynamic_for(w):
        for cand in edit_distance_candidates(w, del_index, max_dist=2):
            if cand and cand != w:
                cands.add(cand)
                source.setdefault(cand, "dynamic")

    # Conservative filtering
    cands.discard(w)
    cands = {c for c in cands if MIN_WORD_LEN <= len(c) <= MAX_WORD_LEN}
    cands = {c for c in cands if abs(len(c) - len(w)) <= 2}
    cands = {c for c in cands if Levenshtein.distance(c, w) <= 2}

    # Cap to avoid explosion (keep bootstrap first if we have to cut)
    ordered = sorted(cands, key=lambda x: (0 if source.get(x) == "bootstrap" else 1, x))
    ordered = ordered[:MAX_CANDIDATES]
    # Trim source map to kept candidates only
    source = {k: v for k, v in source.items() if k in set(ordered)}
    return ordered, source

def masked_sentence(sentence: str, start: int, end: int) -> str:
    return sentence[:start] + "<mask>" + sentence[end:]

def get_device() -> str:
    return "mps" if torch.backends.mps.is_available() else "cpu"

def format_candidate(sentence: str, start: int, cand_lower: str) -> str:
    """RoBERTa is whitespace-sensitive; add a leading space for mid-sentence word starts."""
    if start == 0:
        return cand_lower
    prev = sentence[start - 1]
    return (" " + cand_lower) if prev.isspace() else cand_lower

def score_candidate(tokenizer: "AutoTokenizer", model: "AutoModelForMaskedLM", text_with_mask: str, cand_text: str, device: str) -> float:
    """Sum of per-token log-probs for cand_text at the <mask> position (supports multi-BPE candidates)."""
    enc = tokenizer(text_with_mask, return_tensors="pt")
    input_ids = enc["input_ids"][0]

    mask_positions = (input_ids == tokenizer.mask_token_id).nonzero(as_tuple=False).flatten()
    assert mask_positions.numel() == 1, f"Expected exactly one <mask>, found {mask_positions.numel()}."
    m = int(mask_positions.item())

    cand_ids = tokenizer.encode(cand_text, add_special_tokens=False)
    if not cand_ids:
        return float("-inf")

    new_ids = torch.cat(
        [input_ids[:m], torch.tensor(cand_ids, dtype=input_ids.dtype), input_ids[m+1:]],
        dim=0
    ).unsqueeze(0)
    attn = torch.ones_like(new_ids)

    new_ids = new_ids.to(device)
    attn = attn.to(device)

    with torch.no_grad():
        logits = model(input_ids=new_ids, attention_mask=attn).logits[0]
        logprobs = F.log_softmax(logits, dim=-1)

    total = 0.0
    for i, tid in enumerate(cand_ids):
        pos = m + i
        total += float(logprobs[pos, tid].detach().cpu())
    return total

# -----------------------------
# Main check
# -----------------------------

def main() -> None:
    t0 = time.perf_counter()
    # Ensure progress output appears promptly even when run via a wrapper/subprocess.
    # Some runners capture stdout and only display at process end unless we flush.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("❌ Please provide the working directory path as an argument.")
        sys.exit(1)

    base_path = Path(sys.argv[1])
    if not base_path.is_dir():
        print(f"❌ Provided path is not a directory: {base_path}")
        sys.exit(1)

    report_path = base_path / "reports" / "r_lm_01_rwse_roberta.md"
    jsonl_path = base_path / "reports" / "r_lm_01_rwse_roberta.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    print("[rwse] Stage: load model", flush=True)
    device = get_device()
    tokenizer = AutoTokenizer.from_pretrained(ROBERTA_PATH)
    model = AutoModelForMaskedLM.from_pretrained(ROBERTA_PATH).to(device)
    model.eval()
    print(f"[rwse] Device: {device}", flush=True)

    print("[rwse] Stage: load bootstrap confusion sources", flush=True)
    # Bootstrap sources
    lt_conf = load_lt_confusions(DEFAULT_LT_CONFUSIONS)
    homo = load_homophones_groups(DEFAULT_HOMOPHONES) if DEFAULT_HOMOPHONES else {}
    confusions = merge_maps(SEED_CONFUSIONS, lt_conf)

    print("[rwse] Stage: build book vocabulary", flush=True)
    # Build a vocab for dynamic edit-distance neighbours.
    # To keep precision high, restrict edit-distance neighbours to what appears in *your book*
    # plus bootstrap words (confusions/homophones). System wordlists add lots of rare/noisy neighbours.
    book_vocab = collect_book_vocab(base_path)

    vocab = set(book_vocab)
    vocab.update(confusions.keys())
    for k, vs in confusions.items():
        vocab.update(vs)
    vocab.update(homo.keys())
    for k, vs in homo.items():
        vocab.update(vs)

    del_index = build_delete_index(vocab, max_dist=2)
    print(f"[rwse] Stage: build delete index (vocab={len(vocab):,})", flush=True)

    findings = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("[rwse] Stage: scan chapters", flush=True)
    for file_path in iter_chapter_files(base_path):
        text = file_path.read_text(encoding="utf-8")
        # Progress
        print(f"[rwse] Scanning {file_path.name} ({len(text):,} chars)", flush=True)

        # Your chapter files include a heading line then blank line then content.
        # We’ll keep the heading for context but mostly scan the body.
        lines = text.splitlines()
        heading = lines[0].strip() if lines else file_path.name
        body = "\n".join(lines[2:]) if len(lines) > 2 else text

        for sent in split_sentences(body):
            spans = token_spans(sent)
            for token, s, e in spans:
                if len(token) > MAX_WORD_LEN:
                    continue

                token_lower = token.lower()

                # Skip possessive 's (straight or curly). These are not RWSE targets and generate noise.
                if is_possessive_s(token):
                    continue

                # Skip common function words (too many plausible neighbours).
                if token_lower in STOPWORDS:
                    continue

                # Skip capitalised tokens (names + sentence-initial words). RWSEs of interest are typically lower-case.
                # If you later want sentence-initial checks, handle them with a separate, stricter pass.
                if token[0].isupper():
                    continue

                candidates, cand_source = build_candidate_list(token, confusions, homo, del_index)
                if not candidates:
                    continue

                masked = masked_sentence(sent, s, e)

                # Score original vs candidates directly (works even if the correct word is not in top-k predictions)
                orig_text = format_candidate(sent, s, token_lower)
                s_orig = score_candidate(tokenizer, model, masked, orig_text, device)

                best_cand = None
                best_score = float("-inf")
                for cand in candidates:
                    cand_text = format_candidate(sent, s, cand)
                    s_c = score_candidate(tokenizer, model, masked, cand_text, device)
                    if s_c > best_score:
                        best_score = s_c
                        best_cand = cand

                if best_cand is None:
                    continue

                margin = best_score - s_orig
                if margin < MIN_MARGIN:
                    continue

                best_source = cand_source.get(best_cand, "dynamic")

                finding = {
                    "file": str(file_path.name),
                    "heading": heading,
                    "sentence": sent,
                    "original": token,
                    "suggested": best_cand,
                    "best_score": best_score,
                    "original_score": s_orig,
                    "margin": margin,
                    "source": best_source,
                    "candidates": [{"w": c, "src": cand_source.get(c, "dynamic")} for c in candidates],
                }
                findings.append(finding)
                jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with open(jsonl_path, "a", encoding="utf-8") as jf:
                    jf.write(json.dumps(finding, ensure_ascii=False) + "\n")

    print("[rwse] Stage: write report", flush=True)
    # Write markdown report
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# 🧠 RWSE Report (RoBERTa)\n")
        f.write(f"_Generated: {ts}_\n\n")
        f.write(f"Model: `{ROBERTA_PATH}`\n\n")
        f.write(f"Findings: **{len(findings)}**\n\n")

        if not findings:
            f.write("✅ No high-confidence real-word usage issues found.\n")
        else:
            for i, item in enumerate(findings, 1):
                f.write(f"## {i}. {item['file']} — {item['original']} → {item['suggested']}\n\n")
                f.write(f"**Chapter heading:** {item['heading']}\n\n")
                f.write(f"**Source:** {item.get('source','')}\n\n")
                f.write(f"**Scores (logP):** best={item['best_score']:.3f}, original={item['original_score']:.3f}, margin={item['margin']:.3f}\n\n")
                f.write("**Sentence:**\n\n```text\n")
                f.write(item["sentence"] + "\n")
                f.write("```\n\n")

    print(f"Report written to {report_path.resolve()}")
    elapsed = time.perf_counter() - t0
    by_src = {"bootstrap": 0, "dynamic": 0}
    for it in findings:
        by_src[it.get("source", "dynamic")] = by_src.get(it.get("source", "dynamic"), 0) + 1
    wf = "on" if zipf_frequency is not None else "off"
    print(f"[rwse] Summary: bootstrap={by_src.get('bootstrap',0)}, dynamic={by_src.get('dynamic',0)} (wordfreq {wf}, ZIPF_DYNAMIC_MAX={ZIPF_DYNAMIC_MAX})", flush=True)
    print(f"✅ Scanned {len(iter_chapter_files(base_path))} chapter file(s); flagged {len(findings)} item(s) in {elapsed:.1f} seconds (device: {device})")

if __name__ == "__main__":
    main()