You turn one option from a client rework report into a list of concrete changes ("ops") to a training task.

Op types (fill only the fields each type uses; set the others to null):
- reweight {crit, to}: change a criterion's points to `to` (a number, as a string).
- delete {crit}: remove a criterion.
- add {text, points, category}: add a new criterion.
- reword {crit, new_text}: reword a criterion.
- accept_alternative {crit, value}: the criterion also accepts `value`.
- model_flip {crit, to, reason}: use when the report states the model would now pass ("pass") or fail ("fail") a criterion.
- gold_flip {crit, to, reason}: the same, for the gold deliverable.
- prompt_edit {text}: a sentence added to or changed in the prompt.
- reference_edit {file, location, change, metadata_only}: a change to a reference file. metadata_only is true when only file properties change.
- gold_edit {location, change}: a change to the gold deliverable.
- trajectory_edit {step, change}: a change to a trajectory step, e.g. step "6.4".
- failure_note_edit {idx, change}: a change to failure note number idx.

Rules:
- Use criterion ids exactly as written in the rubric (e.g. "4.15").
- Only include changes this option makes. Bullets that describe the current state, or other options, are not changes.
- When the report says the model would gain or lose points on lines under this option, add a model_flip for each of those lines.
- For "accept both answers", add accept_alternative for each line, and a model_flip when the report says the model would then pass.

## {option_label}
{option_text}

## Impact bullets for this card (scope in brackets)
{impacts}

## Rubric (id, points, text)
{rubric}
