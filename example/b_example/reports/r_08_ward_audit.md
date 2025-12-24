# 🎯 Ward/Wards Style Audit

_Generated: 2025-12-23 21:14:00_  
_Source path: `/Users/graemesheppard/Developer/etna/etna/example/b_example`_

## 🧠 Instructions for AI Reviewer

You are reviewing `-ward` / `-wards` usage in a British English fiction manuscript.
This report only includes paragraphs where at least one -ward/-wards token has been flagged by an **advisory** rule (currently ETNA_WARD_FORWARD_TO_FORWARDS).
Hard style violations like `toward`→`towards`, bare `-ward` adverbs, or `-wards` before nouns are handled in a different report and should be assumed already fixed.

Please follow these house-style principles:

1. **General preference for -wards adverbs**  
   - Prefer forms like **towards, backwards, forwards, upwards, onwards, afterwards**
     when they function as *directional adverbs of motion* (e.g. “He walked forwards into the room”).
   - American forms like **toward** should normally become **towards**.

2. **Context where -ward is fine or preferred**  
   - Fixed expressions or set phrases (e.g. “forward planning”, “backward compatibility”) may keep **-ward**.
   - Uses functioning more like adjectives than adverbs of motion can reasonably be **-ward**.

3. **Special handling for “forward” vs “forwards”**  
   - Treat recommendations from the `ETNA_WARD_FORWARD_TO_FORWARDS` rule as **advisory**, not absolute.
   - **forward** is often acceptable (and idiomatic) in figurative or static uses
     (e.g. “looking forward”, “a step forward in his career”).
   - Use **forwards** especially for clear, literal motion through space.

4. **What to do for each paragraph (advisory cases only)**  
   For every paragraph below:
   - Look at the list of tokens and how LanguageTool has flagged them under advisory rules.
   - Decide for each token whether it is **fine as is** within BrE with a preference for -wards, or whether you would **recommend a change**.
   - If you recommend a change, suggest the exact replacement and **explain briefly why**
     (e.g. “BrE style prefers ‘towards’ here for physical movement”, or
     “adjectival use in a fixed phrase, so ‘forward’ is appropriate”).
   - If LanguageTool did **not** flag a token but you think it clashes with the style guide,
     call that out explicitly; this may indicate the need for a new or refined `ETNA_WARD_*` rule.

5. **Output format suggestion**  
   For each paragraph, a useful structure would be:

   - Bullet list per token:
     - `<token>` – **keep** / **change to <X>** (reason…)
   - Only elaborate where the choice is non-obvious or stylistically important.

## 📊 Summary

- Total paragraphs with at least one `-ward/-wards` token flagged by an advisory rule: **0**

