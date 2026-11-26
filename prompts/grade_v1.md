You are grading one file against part of a rubric for a training task.

Rules:
- Grade only what is in the file below. Do not assume anything that is not shown.
- Criteria that say "displays", "shows" or "shown" are judged on the shown (display) string, not the stored value.
- A calculation criterion passes when the file's value rounds to the stated figure at the display precision the prompt asks for.
- Positive criteria get result "pass" or "fail".
- Penalty criteria (negative points, category "Penalty") get "triggered" only when the problem is clearly present; otherwise "not_triggered".
- Evidence: name the cell references (or paragraph/page ids) you relied on and the values as shown. Keep it short.
- Confidence: "high", "medium" or "low". Use "low" when the file doesn't clearly settle the criterion.
- Return one entry for every criterion listed, using its id exactly.

## Task prompt
{prompt}

## Rubric section {subtask}: {title}
{criteria}

## File: {file_name}
{file_text}
