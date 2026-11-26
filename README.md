# Task Rework Dashboard

A Streamlit app for assessing client rework reports on training tasks (Step 1) and verifying the revised tasks (Step 2). The build spec is the source of truth. `DECISIONS.md` records the choices made where the spec is silent.

The dashboard only reads task files. People make the fixes; the app checks them. The one file it writes is a decisions workbook, a new copy of the uploaded report.

## Run it on a Windows computer

1. **Install Python (once).** Download Python 3.11 or newer from <https://www.python.org/downloads/>. On the installer's first screen tick **"Add python.exe to PATH"**, then click **Install Now**.
2. **Start the app.** In the app's folder, double-click **`run_windows.bat`**.
   - The first time, it installs what the app needs (a few minutes) and opens a settings file called `.env` in Notepad.
   - In `.env`, set `APP_PASSWORD=` to a password of your choice, and paste your OpenRouter key after `LLM_API_KEY=`. Save and close Notepad, then double-click `run_windows.bat` again.
3. Your browser opens at <http://localhost:8501>. Sign in with your name and the password.
4. Keep the black window open while you use the app. Close it to stop the app.

**Many tasks at once:** on **Upload → Bulk upload**, select all your task zips, rework reports and QA sheets together (or one zip that holds them all) and click **Check files**. Files are matched by the task number in their names, and each version label comes from the report's "flagged at" line. Tasks marked *ready* are saved with **Save ready tasks**; any task the app is unsure about opens a short box to confirm its roles or label. The **Overview** page then shows every task's stage, open flags, parts and categories, with filters, plus every card across all tasks.

Your data (uploads, grades, decisions) is kept in the `data` folder next to the app. The installed packages live in `%LOCALAPPDATA%\TaskReworkDashboard`, outside OneDrive.

Without an `LLM_API_KEY` everything still works except AI grading, AI reading of QA comments and AI checks; those parts fall back to rules or ask a person.

## Deploy on Railway

1. Sign in at <https://railway.com> with GitHub and choose **New Project → Deploy from GitHub repo → jashish18/fulkrum** (pick the branch you want to run).
2. Railway finds the `Dockerfile` and `railway.json` and builds the app. It runs as **one instance only**, because the SQLite database can't be shared.
3. **Add a volume** (right-click the service → *Attach volume*) with mount path **`/data`**. Without it, every redeploy loses your data.
4. Under **Variables**, add:
   - `APP_PASSWORD`: the team password (the app refuses to start without one)
   - `LLM_API_KEY`: your OpenRouter key
   - optional: `GRADER_MODEL`, `LLM_ZDR` (default `true`), `MODEL_THRESHOLD` (0.95), `GOLD_THRESHOLD` (0.99)
5. Under **Settings → Networking**, click **Generate Domain**. Open that address and sign in.

`PORT` and `DATA_DIR=/data` are set for you. The grader model can also be picked later on the app's Settings page.

## Run with Docker anywhere

```bash
docker build -t task-rework-dashboard .
docker run -p 8501:8501 -v trd-data:/data -e APP_PASSWORD=... -e LLM_API_KEY=... task-rework-dashboard
```

## For developers

```bash
pip install -r requirements.txt
cp .env.example .env   # then set APP_PASSWORD and the LLM settings
streamlit run app.py
python -m pytest -q    # acceptance tests use samples/ (gitignored) and skip without it
```

Layout: `app.py` (portfolio), `pages/` (Upload, Versions, Cards, Grades, Task, Rules, Settings), `core/` (ingest, extract, report_parser, grading, scoring, impact, verify, writeback, llm), `db/`, `prompts/` (versioned LLM prompts), `tests/`.

## Status

- [x] P1 Ingest and extraction
- [x] P2 Report parser
- [x] P3 Grading and review (real grading needs an OpenRouter key)
- [x] P4 Step 1 scenarios
- [x] P5 Write-back
- [x] P6 Verification and integrity
- [x] P7 Deployment
