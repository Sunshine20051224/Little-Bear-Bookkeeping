# Little-Bear-Bookkeeping

A personal expense tracking and budget alert application built with multi-paradigm programming principles, covering data persistence, pure-function business rules and aggregation, asynchronous maintenance and backup checks, and an interactive frontend/backend interface.

- [GitHub Repository](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping)
- [Frontend App · Streamlit Community Cloud](https://little-bear-bookkeeping-2ppuqrbguqmlayds7pcqkn.streamlit.app/)
- [Backend API Docs · Render](https://little-bear-bookkeeping.onrender.com/docs) · [Health Check](https://little-bear-bookkeeping.onrender.com/health)

---

## 1. System Architecture and Project Layout

The project uses a modular layered design that decouples database sessions and models, pure-function rules and summary calculation, asynchronous maintenance control, frontend/backend entry points, and automated tests:

Little-Bear-Bookkeeping/
├── .github/workflows/       # GitHub Actions CI workflow configuration
├── backend/                 # Backend API / controllers and service wrappers
├── data/                    # Sample data (e.g., sample_data.json)
├── database/                # Database models (models.py) and session management (session.py)
├── frontend/                # Frontend UI (app.py, Streamlit application)
├── maintenance/             # Maintenance, write guard, backup, and cloud replacement workflow
├── processing/              # Core business rules (rules.py) and summary service (service.py)
├── tests/                   # Unit tests (test_unit.py) and integration tests (test_integration.py)
├── .env.example             # Environment variable example template
├── .gitignore               # Git ignore rules
├── README.md                # Project documentation and engineering records
├── requirements.txt         # Python dependency list
└── start.py                 # Local / unified entry point

Users enter a category and amount in the Streamlit form. The frontend sends JSON to FastAPI, the backend queries current-month spending for that category via SQLAlchemy, `BudgetAdvisorService` invokes the budget rules, writes the expense along with its recorded alert status to the database, and returns the result to the frontend. The page refreshes charts and the expense list through the API. The current demo uses a single ledger and is suited to synthetic data.

## 2. Local Setup (Windows PowerShell)

Run commands from the repository root. `.python-version` specifies Python 3.12.7, while GitHub Actions currently uses Python 3.11. Virtual environments bundled in old archives retain the original interpreter path and should be recreated on a new machine. The commands below install dependencies into `venv` without relying on PowerShell activation scripts.

py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe start.py

Open [Local Frontend](http://localhost:8501) or [Local API Docs](http://127.0.0.1:8001/docs), and stop the launcher with Ctrl+C. To run them separately, start each command in its own terminal and stop each with Ctrl+C. If you change ports, update `BACKEND_URL` for the frontend accordingly.

.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
.\venv\Scripts\python.exe -m streamlit run frontend/app.py --server.port 8501

Local default runs use Python's built-in SQLite driver, while connecting to cloud or local PostgreSQL 16 uses `psycopg2-binary` from `requirements.txt`. The backend creates tables on startup, defaulting to `expense_tracker.db`. `DATABASE_URL` selects the database, while `DEFAULT_MONTHLY_BUDGET`, `BUDGET_FOOD`, and related variables configure default and category budgets, and `BUDGET_WARNING_RATIO` sets the warning threshold. See [.env.example](.env.example) for sample settings. When deploying the frontend, set the entry point to `frontend/app.py` and configure `BACKEND_URL` to the public Render backend URL in Streamlit settings. Render Web Service uses the repository root and start command `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`. Store database connection values only in private environment settings.

## 3. Monthly Budget Walkthrough

Expenses come from user input. The [three synthetic expenses](data/sample_data.json) in the repository can be submitted manually. Using an empty local demo database, the same UTC calendar month, and a default Food budget of 1500, submitting them in file order produces the table below. Repeating a submission adds another expense. Historical records remain queryable across months.

| Added Amount | Monthly Total | Expected Status | Reason |
| ---: | ---: | --- | --- |
| 300 | 300 | NORMAL | 20% used |
| 900 | 1200 | WARNING | Reached the 80% warning threshold |
| 350 | 1550 | EXCEEDED | Exceeded budget by 50 |

Amounts are recorded in minor currency units (cents), and single-entry evaluation shares the same status logic as category summaries. With a budget of 1500, 1199.99 remains NORMAL and 1500.01 is EXCEEDED; status is evaluated from amounts first before formatting percentages. When a category budget is zero, zero spending shows NORMAL, while any positive new expense is counted as exceeded, with `null` ratio in the API and `N/A (zero budget)` in the UI.

You can also run the following PowerShell snippet after starting the local backend. Set a dedicated SQLite file in `.env` first, such as `DATABASE_URL=sqlite:///./demo_expense.db`, so demo records are stored there.

$demoRecords = Get-Content -Raw data/sample_data.json | ConvertFrom-Json
foreach ($record in $demoRecords) {
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8001/expenses' -ContentType 'application/json' -Body ($record | ConvertTo-Json -Compress) |
        Select-Object updated_spent, status, over_amount
}

| Endpoint | Purpose |
| --- | --- |
| `POST /expenses` | Add an expense and return its persisted ID, budget status, and over-budget amount |
| `GET /expenses` | Fetch historical expenses in reverse order, optionally filtered by `?category=Food` |
| `GET /expenses/{expense_id}` | Fetch details by ID, returning 404 if missing |
| `GET /summary` | Return current-month totals, budgets, and statuses by category for charts |

## 4. Code Walkthrough and Design Trade-offs

- **Procedural**. In [backend/main.py](backend/main.py), `create_expense` follows a sequential flow of querying monthly spend, evaluating status, persisting to the database, and returning the response; `get_db` in [database/session.py](database/session.py) manages transaction commit, rollback, and session closing.
- **Functional**. `evaluate_budget_status` and `summarize_expenses_by_category` in [processing/rules.py](processing/rules.py) compute results purely from passed values and records. Deterministic inputs yield deterministic outputs that can be verified directly against the table above.
- **Object-oriented**. `BudgetAdvisorService` in [processing/service.py](processing/service.py) holds category budgets and warning ratios, exposing single-expense evaluation and category summary methods. FastAPI obtains this service via dependency injection.

**Months and history**. Monthly totals are grouped by the UTC calendar month of each expense's recorded timestamp. Prior-month entries for the same category remain queryable without wiping tables at month start or depending on server local time zones. This design does not support closing months by user time zone or backdating expense timestamps.

**Persisted alert status**. Each expense stores the alert label evaluated when it was created, while the summary view recomputes status against current budgets. After a budget adjustment, item details still show the original label. Full historical reason reconstruction would also require storing historical budgets and warning ratios, whereas the table currently stores the expense and its label. Hitting the budget limit exactly yields WARNING, and exceeding it yields EXCEEDED.

## 5. Tests and CI Records

Run `python -m pytest` from the project root using the project virtual environment. The [test suite](tests/) covers parameterized budget rules, maintenance triggers, and API integration tests against isolated SQLite databases. CI runs on push and pull request events by installing pinned dependencies and executing the same test command. Local verification results and public CI runs for pushed commits are listed below.

On 2026-11-23, commit `778fdb4` pushed to `main` passed 34 tests locally on Python 3.11 with 1 dependency deprecation warning, covering monthly budgets, API persistence, and SQLite recovery; its GitHub Actions run also passed. [CI run for 778fdb4](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45159557990)

Commit `9c4da25` passed all 95 tests in the full suite, covering read-only request retries during free-tier backend cold starts, due checks that still fire after maintenance completion, resuming interrupted controller runs, and recovery after explicit cloud request rejection. All 95 tests passed locally against SQLite and an isolated PostgreSQL 16.16 instance, and also passed in GitHub Actions using Python 3.11 and a PostgreSQL 16 service with per-test schemas for PostgreSQL write-guard tests. [CI run for 9c4da25](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45165700440)

On the separate `debug/ci-fault-demo` branch, commit `6132f4f` changed the over-budget check from `usage_ratio > 1.0` to `> 10.0`, causing an existing case with 1010 spent against a 1000 budget to be classified as WARNING. Commit `970a536` restored the original threshold and returned tests to green. [Fault CI run](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45039678262) · [Post-fix CI run](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45043678181) · [Mainline 6edba96 CI run](https://github.com/Sunshine20051224/Little-Bear-Bookkeeping/actions/runs/45132165957)

## 6. Cloud Database and Maintenance Operations

### 6.1 Two Online Persistence Checks

**On 2026-11-23, after switching to Render Postgres 16, the same synthetic expense survived a backend redeployment.** Times below are in Beijing Time (UTC+8).

| Database and Time | Before Redeploy | After Redeploy |
| --- | --- | --- |
| Ephemeral SQLite, 18:48 | `Other` synthetic expense `id=1`, amount `1.23` | After redeploying the same commit `6edba96`, querying by ID returned 404, the list was empty, and `Other` monthly spend dropped to `0`. |
| Render Postgres 16, 19:49–19:51 | Added `Food` synthetic expense `id=1`, amount `12.34` via web API on deployment `dep-dc22fpmc6qcc73c7jjog` | New deployment `dep-dc22gnnf3r2c73e520rg` read back all six fields from the same database; `Food` monthly total remained `12.34` against budget `1500` (`NORMAL`), with the other four categories at `0`. |

During the first check, the free web service stored data in an ephemeral SQLite file under default settings, resulting in an empty ledger in both Streamlit and the backend after redeployment. After confirming 0 SQLite rows at 19:47, the backend was connected to a free Render Postgres 16 instance in Virginia. The second check queried `/expenses`, `/expenses/1`, and raw PostgreSQL rows, and compared `/summary` across both deployments with matching budget fields across all five categories and SHA-256 digests used for sample notes in public records. [SQLite pre-redeploy record](docs/evidence/persistence-before-20261123.json) · [SQLite post-redeploy record](docs/evidence/persistence-after-20261123.json) · [PostgreSQL before/after comparison](docs/evidence/persistence-postgres-20261123.json)

After the second redeployment, the online Streamlit budget chart loaded with `Food` at `12.34` out of `1500` (`0.8%` used), and filtering by `Food` showed a single row (`id=1`, amount `12.34`, status `NORMAL`) matching the backend readback. [Frontend observation and backend verification record](docs/evidence/persistence-postgres-20261123.json)

On 2026-11-28, a full cloud database replacement drill was completed under a single-instance quota (`DB_INSTANCE_LIMIT=1`). After local isolated trial-restore verification passed, the original source instance `dpg-dc225qk9v7es73c2bv50-a` was deleted with explicit authorization and the backend service switched to the new instance `dpg-dc5c10mc6qcc738gtvdg-a`. See Section 6.3 for drill details. [Render Free Database Lifecycle Docs](https://render.com/docs/free)

### 6.2 Read-Only Diagnostics and Scheduled Checks

Set `RENDER_API_KEY` and backend `RENDER_SERVICE_ID` in the controller's private `.env`. The command below inspects the specified web service's database configuration and outputs only the detected database type and provider (or `unknown` if unrecognized or missing on the service and its environment groups). Neither frontend nor Render Dashboard hostnames alone identify the database provider.

.\venv\Scripts\python.exe -m maintenance.render_provider diagnose

The cloud adapter currently supports **Render Postgres**. Once the actual database and resource permissions are verified, set `MAINTENANCE_PROVIDER=render-postgres`, `SOURCE_DB_ID`, and `RENDER_OWNER_ID` in the private config, and point `BACKEND_URL` to the deployed backend. Controller and backend should share the same category budgets and warning thresholds. Run a one-shot check or continuous watch in a separate terminal.

.\venv\Scripts\python.exe -m maintenance.workflow check
.\venv\Scripts\python.exe -m maintenance.workflow watch

`check` performs a single asynchronous check, while `watch` polls the active instance every `CHECK_INTERVAL_SECONDS` until stopped with Ctrl+C. The unified `start.py` entry point starts read-only `watch` automatically when Render monitoring variables are configured, without needing a second process. Local bookkeeping demos still work without cloud monitoring enabled, and neither monitoring nor app startup grants replacement or deletion authority.

### 6.3 Database Replacement Workflow and Drill Record

1. Select a dedicated source instance containing only synthetic demo data, and verify source, workspace, and backend IDs, source readability, network permissions, real quota, and backup host disk space. Set `TARGET_DB_PLAN`, `TARGET_DB_REGION`, `TARGET_DB_VERSION`, and `DB_INSTANCE_LIMIT` (`1` or `2`) as needed. For time-triggered runs, `MAINTENANCE_AT_UTC` uses an offset-aware ISO 8601 timestamp (such as ending with `+00:00`), and `MAINTENANCE_WAIT_SECONDS` caps wait time. The backend must first run a version supporting database write protection.
2. When only one cloud instance slot is allowed, install PostgreSQL locally with the same major version as the source and target, create a dedicated **empty database** locally (such as `little_bear_trial`), and point private `TRIAL_DATABASE_URL` to its loopback URL. The controller uses `psycopg2-binary` pinned in `requirements.txt` to back up and restore via SQLAlchemy and JSON without requiring `pg_dump`. Two-instance runs keep the source database until the new instance is verified and switched. [PostgreSQL Windows Installer](https://www.postgresql.org/download/windows/)
3. Run the command below only when explicitly authorized to replace and delete the **specified source instance**. Generate a run ID on the first run and keep it together with the three verified resource IDs so they match the private configuration.

$runId = [guid]::NewGuid().ToString()
$sourceId = Read-Host 'Database instance ID authorized for replacement and final deletion'
$backendId = Read-Host 'Render Web Service ID authorized for switchover'
$workspaceId = Read-Host 'Workspace ID owning those resources'
.\venv\Scripts\python.exe -m maintenance.workflow run --run-id $runId --authorize-source $sourceId --authorize-backend $backendId --authorize-workspace $workspaceId --authorize-delete $sourceId

`run` checks maintenance trigger conditions, waits for in-flight write transactions to finish, blocks normal writes, and backs up current expenses. Single-slot mode must restore and verify the final backup in the isolated trial database first; two-slot mode retains the source until the target passes verification. The controller calls the provider API to create the target, records its instance ID, restores and verifies all expense fields, reference-month budget results, and the next record ID. It then updates the backend database connection, waits for deployment completion, queries the deployed API, writes a controlled probe record with a random note to the target, reads it back by ID via the API, and cleans it up. Normal writes return HTTP 503 during maintenance and resume only after target creation and verification states are persisted.

**Interruptions and logs.** The default directory is `.little-bear-maintenance` under the controller user's home directory, or a separate directory outside the source and backend via `MAINTENANCE_STATE_DIR`. `state.json` tracks the active instance and current stage, `monitor.jsonl` logs read-only checks, `<run_id>.jsonl` logs the maintenance run, and `backups/<run_id>.json` stores source ID, reference time, and current expenses. On failure, inspect the recorded stage, last event, and resource state while keeping backups, state files, budget settings, and the trial database intact, then resume with the **same run ID and resource IDs**. If creation outcome is unclear, inspect the run-tagged resource first rather than retrying creation with a new ID.

**Course evidence (2026-11-28 live single-slot replacement drill).** Following sanitization guidelines, raw backup files, private connection strings, API tokens, and full state directories are not committed. Key redacted milestones from the completed run are recorded below ([workflow](maintenance/workflow.py) · [write-guard tests](tests/test_write_guard.py)):

- **Run ID (`run_id`)**: `15250c95-c567-47be-877d-52f8c62820f4`
- **Isolated trial restore**: Verified restore of 2 expenses and monthly budget summary against empty local PostgreSQL 16 database `little_bear_trial` (`trial_verified: true`, `restored_count: 2`, `contents_verified: true`, `monthly_rule_verified: true`)
- **Source deletion and target creation**: Source `dpg-dc225qk9v7es73c2bv50-a` deleted (`source_deleted: true`), and new target `dpg-dc5c10mc6qcc738gtvdg-a` created and restored
- **Backend cutover and probe verification**: Render service `srv-dc1lj4jl550s73erkk1g` switched to deployment `dep-dc5c1j8u01pc73873g90`, with readback and controlled write probe passing (`backend_read_verified: true`, `controlled_write_verified: true`, `probe_id: 4`)
- **Final state**: `complete` (`maintenance_mode: false`), with `dpg-dc5c10mc6qcc738gtvdg-a` as the active instance

## 7. Data, Tools, and Extensions

Expenses are entered manually by users, and demos and tests use synthetic records. The three Food examples were written for this course review with inputs and expected outputs listed in Section 3. The project uses FastAPI, Streamlit, SQLAlchemy, Pydantic, and HTTPX, and calls Render service and Postgres APIs for cloud maintenance. [Render API Reference](https://api-docs.render.com/reference) · [Project Dependencies](requirements.txt)

OpenAI Codex was used during this review against course requirements to assist with amount and month logic refinements, maintenance workflow, tests, examples, and documentation, alongside verification across real APIs and isolated databases, local HTTP Streamlit form submission, GitHub Actions failure/fix runs, and concurrent write locks, recovery, and persistence in PostgreSQL. Any AI tools, external code, or examples from earlier development stages should be noted according to actual usage. None of the four optional bonus features (multithreading, web scraping, email notifications, or Docker) are claimed.