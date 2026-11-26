You read a QA reviewer's comment on one card of a client rework report and say which option it chooses.

Answer with:
- option_key: "1", "2" or "3" for an option, "fix" to apply a proposed fix, "keep" to change nothing, "new" if the comment proposes something else, or "unclear".
- scope_criteria: criterion ids the choice is limited to (e.g. ["6.13","6.14","6.15"]), or [] if it applies to the whole card.
- new_option_text: the proposed change when option_key is "new", else "".
- candidates: every option key the comment could mean.
- confidence: high, medium or low.
- ambiguity_reason: why it can't be mapped to one option, or "".
- rationale: one sentence.

Guidance:
- "Agree with defense" / "Aligned with defense" means the option that matches the prior stance. If the prior stance was a defence or objection, that is usually the keep-as-is option.
- "This is OK" / "Keep" means keep.
- "Use Option N for X only" means option N, scoped to X.
- "Either A or B" is unclear; list both candidates.
- On a Part 1b or 1a card, "Agree" means apply the fix and "Disagree" means keep.

## Card {code} (Part {part})
{options}

## Prior stance (previous round)
{stance}

## QA comment
{qa}
