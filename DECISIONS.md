# Decisions

Where the spec is silent, the simplest option was chosen and recorded here.

## Phase 1 – Ingest and extraction

1. **Single top folder.** If every file in a zip sits under one top-level folder (common when a folder is zipped), that folder is dropped from file names, so names look like `prompt.txt`, not `Task 45 v36/prompt.txt`.
2. **Nested zips** become folders named after the zip (e.g. `Reference files/Memo.docx`). Nesting is followed up to 5 levels. `__MACOSX`, `.DS_Store`, `Thumbs.db` and Office lock files (`~$…`) are ignored.
3. **Role matching** compares file names case-insensitively, without extension and ignoring spaces, `_` and `-`, so a rubric source of `Gold Model` matches `gold_model.xlsx`.
4. **Role order.** rubric → trajectory → failure notes → prompt → gold (rubric) → reference (rubric) → reference (inside a folder named *reference* or *input*) → model output (any other xlsx/docx/pptx/pdf) → reference (anything else). The sample zips carry the prior QA sheet inside a `QA feedback/` folder, so an extra role `qa_sheet` catches `qa_feedback…` files and anything in that folder (otherwise it would be mistaken for the model output). An extra role `other` lets the user park files that are none of these.
5. **Task number** is prefilled from `task <n>` in the zip name, or from a leading number (`45-surety-bonds….zip`), or from the zip's top folder; the user can edit it. Tasks are keyed by this number. The sample zips have no `v<n>` in their names, so the version label must be typed on upload.
6. **Same zip uploaded twice** for the same task reuses the saved version (matched by sha256). Role corrections on re-upload are applied to the saved version.
7. **Version label clash.** A different zip with an existing label for the same task is refused with a message, rather than overwriting.
8. **Personal names in metadata.** A property value (creator, lastModifiedBy, Company, Manager, PDF Author) is flagged when it is 1–4 alphabetic words and not a known tool or placeholder (openpyxl, Microsoft Office User, etc.). This errs towards flagging; the user reviews the list.
9. **Probable `####`** is only checked for numbers and dates, because Excel lets text spill into the next cell. It is flagged when the shown text is more than one character wider than the column (or merged range). Column widths in Excel units are treated as characters; widths set on a range of columns apply to every column in it.
10. **Display rendering** uses round-half-up (as Excel does), handles `;` sections for negatives and zero, `[Red]`-style tags, currency prefixes and thousands scaling (`#,##0,`). Unknown date formats fall back to ISO dates.
11. **Recalculation.** When formula cells have no cached value, the file shows a "recalculation needed" warning. `core.extract.recalc_with_libreoffice` is available if LibreOffice is installed; it is not run automatically during upload because it can be slow on large workbooks.
12. **pptx text** is read straight from the slide XML because python-pptx is not in the stack.
13. **Failure notes** are split into items by numbered lines (`1.`, `1)`, `Failure 1:`), else bullets, else blank-line paragraphs. Checked on the samples: task 45 gives 2 numbered notes, task 35 gives 4 bulleted notes.
14. **Extractions** are saved as JSON under `DATA_DIR/versions/<id>/extract/`; `files.text_path` holds the path relative to `DATA_DIR`.
15. **Python version.** The spec says 3.11; the code avoids anything newer and the Dockerfile will use `python:3.11-slim`. Tests were run here on Python 3.13.

## Phase 2 – Report parser

1. **Summary line** is read from the first text cell of the `Task <id>` sheet (falling back to `Part 2`). The import fails with a message if the total rows/groups or any per-part count differs from what was parsed.
2. **Defense quotes.** The spec names curly quotes; the sample reports use straight quotes. Both are accepted. A quote ends only where the next `QA #` entry (or the cell) begins, so quotes inside a comment don't cut it short.
3. **Section labels** are matched only from the fixed list in the spec, at the start of a line. `Hence:` is also matched mid-line because the reports put it at the end of the Issue paragraph. Other prose with a colon ("But nothing says so:") is left alone.
4. **"Clear fix:" vs "Proposed fix:".** In the samples, a 1b row always has "Proposed fix:" for the fix itself, and "Clear fix:" / "Ask the expert:" as the judgement line. The fix text is taken from "Proposed fix:", then "Clear fix:", then "Fix:" (1a rows).
5. **Folded Part 2 rows** sometimes carry a proposed fix instead of options (e.g. task 35, Category 2, row 39 under card 2.1). These count as parsed; they follow the lead row's decision.
6. **Card options** are the lead row's options. Folded rows keep their own parsed text too.
7. **Card links.** `decide_together` links are stored both ways. A row folded under a row of another card adds a `folded` link (none occur in the samples). Every "Category N, row M" cross-reference to a row in another card adds a `mentions` link.
8. **Criteria ranges** like "[6.12] to [6.15]" are also expanded into the full list, kept beside the plain matches.
9. **Bare cell references** skip tokens that look like codes rather than cells (FY26, Q1, AG-B2, S26-0363…). A few false hits may remain; they are only used as hints.
10. **Prior QA sheet** rows are stored in an extra table `prior_qa_items` (the spec's data model has no table for them). A QA sheet found inside the task zip is loaded automatically; it can also be uploaded on its own.
11. **LLM fallback** runs only for rows the regex parser couldn't complete. Both sample reports parse fully without it. If no LLM key is set, those rows are kept as raw text and a warning is shown.
12. **LLM responses** are cached as JSON files under `DATA_DIR/llm_cache/`, keyed by a hash of model, prompt version, messages and schema, because `llm_calls` has no column for the response.
13. **Re-importing** the same report (same task, file name and flagged version) replaces its cards and rows rather than adding a second copy.

## Phase 3 – Grading and review

1. **Grader model is chosen in Settings** from a dropdown of the models your OpenRouter key can use (fetched from OpenRouter's `/models/user`, then `/models`, with prices shown). If the list can't be fetched, a text box takes a model id. `anthropic/claude-opus-5.5` is only the fallback default.
2. **Settings live in the database** (`settings` table) and override the environment defaults. Every change is written to the audit log.
3. **Grade storage.** Each criterion gets one `grades` row per pass. The pass-1 row carries the confirmed `final_result` and `reviewed_by`.
4. **Result names.** Positive criteria are pass/fail and penalties triggered/not_triggered. If the grader mixes these up, "fail" on a penalty is read as triggered and "not_triggered" on a criterion as pass.
5. **Review flags.** A line goes to review when the two passes disagree, either pass has low confidence, a pass left it out, or (for the gold) it fails or a penalty triggers. Gold lines are labelled "grader error or real gold issue".
6. **Confirm all unflagged lines** is a shortcut that accepts the agreed result for every unflagged line. Flagged lines must be confirmed one by one. The baseline can be locked only when every line is confirmed. Unlocking is allowed and is audited.
7. **Score before review** uses the agreed (suggested) result for lines not yet confirmed, so a score shows straight after grading.
8. **Cache.** Results are reused when the full request is identical: file text, rubric section, prompt, model and prompt version. This matches the spec's key (file hash, rubric hash, model, prompt version) in effect.
9. **Long sheets.** Sheets over 300 rows are sent as header plus row counts, as the spec says. Targeted lookups into the full data aren't built yet; the gold and model outputs in both samples are well under 300 rows.
10. **Score formula** setting offers "(earned − penalty) / positive_total" (default) and "earned / positive_total". Earned / positive total is always shown too.

## Phase 4 – Step 1 scenarios

1. **Where ops come from.** The LLM suggests ops per option (button on each option). Without an LLM, simple rule-based ops are added automatically for prompt and reference edits the parsed impacts already state, so the re-run badge works anyway. Ops can be added by hand as JSON, confirmed or removed; every user change is audited. Ops sources are `llm`, `rule` and `user` (the spec lists only `llm` and `user`).
2. **Unconfirmed ops count** in projections, so the effect shows straight away; each op shows whether it's confirmed.
3. **Baseline** is the version whose label matches the report's flagged version, else the latest upload. Grades use the confirmed result where set, otherwise the grader's agreed result, with a note that the baseline isn't locked.
4. **Decision keys.** Options 1–3, `fix`, `agree` (apply the fix), `disagree` / `keep` (no change), and `new` (a new option the user writes and gives ops). A Part 2 option whose text starts "Keep" or "Leave" counts as keep.
5. **Decision sources.** The card's form saves `final` when "Final" is ticked, otherwise `expert` (a working call). Unticking Final removes the earlier final call. An Expert call written in the report is imported as `expert`. Precedence follows the spec: final, then expert, then the QA reading.
6. **Scope** limits a decision's criterion ops to the listed criteria. Ops that name no criterion (prompt, reference) still apply.
7. **Added criteria** are assumed failed by the model and passed by the gold unless the op says otherwise (`model_result` / `gold_result`). The task page shows this assumption.
8. **accept_alternative** compares numbers in the model's grade evidence with the alternative at the alternative's decimal places (percent-aware). When the evidence has no number, the page asks the user to set the op's `model_result`.
9. **Failure-note backing** is the points of mapped criteria the model still fails (plus triggered penalties). A note is *holds* at baseline backing, *weakened* below it and *gone* at zero.
10. **QA reading.** The LLM reads QA comments when configured. A rule-based reader of the spec's guidance phrases runs otherwise, and its ambiguity warning is kept if the LLM sees none. "Agree/Aligned with defense" maps to the keep option only when every prior stance is Objected, Defended or Kept. Otherwise it is AMBIGUOUS, which is why task 35's 2.5, 2.6, 2.7 and 2.9 (prior stance "Fixed") are flagged.
11. **BREAK on a card** means removing that card's decision would remove at least one of the task's break conditions.
12. **CONFLICT** covers three cases. (a) Two cards' decided ops hit the same criterion, step, prompt or reference cell/row with different effects. (b) A card kept as is, while another card changes something one of the kept card's other options would have changed. For example, 2.12 keeps the S26-0363 record, which its Option 3 would change, and 1b.13 changes it. (c) Folded or "decide together" cards decided in opposite directions (keep vs change).
13. **REVERSAL** lists the refusing prior stances (Objected, Defended, Kept) whose comment names a criterion in the decision's scope, or names none. Saving such a decision requires a note.
14. **Batch by rule** saves a working (expert) decision on each card with that rule, skipping cards that don't have the chosen option. Each card is then reviewed and made final on the Cards page.
