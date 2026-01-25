import torch
import torch.nn.functional as F
from transformers import AutoTokenizer, AutoModelForMaskedLM

model_path = "/Volumes/Projects/Developer/hf_models/roberta-large"
tok = AutoTokenizer.from_pretrained(model_path)
mdl = AutoModelForMaskedLM.from_pretrained(model_path).to("mps")
mdl.eval()

def score_candidate(text_with_mask: str, cand: str) -> float:
    """
    Scores cand at the <mask> position as sum of per-token log-probs.
    Works for 1+ BPE tokens.
    """
    # Encode original text (contains exactly one <mask>)
    enc = tok(text_with_mask, return_tensors="pt")
    input_ids = enc["input_ids"][0]

    # Locate the single mask token
    mask_positions = (input_ids == tok.mask_token_id).nonzero(as_tuple=False).flatten()
    assert mask_positions.numel() == 1, f"Expected exactly one <mask>, found {mask_positions.numel()}."
    m = int(mask_positions.item())

    # Encode candidate WITHOUT specials; keep RoBERTa whitespace behaviour
    cand_ids = tok.encode(cand, add_special_tokens=False)
    assert len(cand_ids) >= 1, "Candidate encoded to empty token list."

    # Build new input_ids: replace the single mask token with the candidate token IDs
    new_ids = torch.cat(
        [input_ids[:m], torch.tensor(cand_ids, dtype=input_ids.dtype), input_ids[m+1:]],
        dim=0
    ).unsqueeze(0)

    # Attention mask is all ones for this constructed sequence
    attn = torch.ones_like(new_ids)

    # Move to device
    new_ids = new_ids.to("mps")
    attn = attn.to("mps")

    # Compute log-prob for each inserted token at its position (teacher forcing)
    # For MLM, each position is predicted from the full context; we just read off the probability
    # of the token that is actually there.
    with torch.no_grad():
        logits = mdl(input_ids=new_ids, attention_mask=attn).logits[0]  # [seq_len, vocab]
        logprobs = F.log_softmax(logits, dim=-1)

    total = 0.0
    for i, tid in enumerate(cand_ids):
        pos = m + i
        total += float(logprobs[pos, tid].detach().cpu())
    return total

text = "With him holding his <mask>, Mason’s never felt so free."

for w in [" leash", " leach", " hand"]:
    print(w, score_candidate(text, w))