# Task Rework Dashboard

A Streamlit app for assessing client rework reports on training tasks and verifying the revised tasks. The build spec is the source of truth; see `DECISIONS.md` for choices made where the spec is silent.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # then set APP_PASSWORD and the LLM settings
streamlit run app.py
```

## Tests

```bash
python -m pytest -q
```

Acceptance tests use the files in `samples/` (gitignored) and are skipped when it is missing.

## Status

- [x] P1 Ingest and extraction
- [x] P2 Report parser
- [x] P3 Grading and review (real grading needs an OpenRouter key)
- [x] P4 Step 1 scenarios
- [x] P5 Write-back
- [ ] P6 Verification and integrity
- [ ] P7 Deployment
