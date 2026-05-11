# ContractIQ

ContractIQ is an AI-assisted contract review proof-of-concept built on **Temporal** + **FastAPI**. Upload a contract, get a deterministic (mock) AI risk analysis, then approve / request revision / escalate — with an audit timeline.

## How it works

```
Upload PDF/DOCX
   │
   ▼
FastAPI (API + UI)  ──► starts Temporal workflow
   │
   ▼
Temporal worker runs activities:
  - ingest file
  - extract clauses (mock analyzer)
  - score risk (0–100)
  - notify reviewer (email + Slack webhook)
  - wait for human decision (approve / revise / escalate)
```

## Setup (local dev)

### Prerequisites

- Python 3.9+
- Docker (only if you want to run the full stack)

### Install (only via `requirements.txt`)

Create a virtualenv and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## URLs (when running the stack)

- Web UI + API: `http://localhost:3000`
- Temporal UI: `http://localhost:8080`

## Required PDF

Sample contract for testing: (temporal-poc-1/data/sample_contract.txt)
`sample_contract.txt`

## Environment variables

Create a `.env` in the repo root (it is gitignored) -> see `.env.example` for a fuller template.

## Start the stack (optional)

```bash
docker-compose up --build
```

## Run tests (unit tests only)

All tests in `tests/` are unit tests and require **no** Postgres / Temporal services.

```bash
python3 -m pytest -v
```

## Useful commands / logs

```bash
docker-compose logs -f worker       # worker activity execution
docker-compose logs -f api-server   # API requests
docker-compose ps                   # check service health
docker-compose down                 # stop stack
docker-compose down -v              # stop stack + wipe DB volume
```

Key worker log lines:

- `EMAIL SENT` / `EMAIL MOCK SENT`
- `SLACK BLOCK KIT WEBHOOK POSTED` / `SLACK WEBHOOK MOCK`
- `JIRA TICKET CREATED`

## Project layout

```
├── api_server/
│   ├── main.py                 # FastAPI app + REST endpoints
│   └── static/index.html       # Single-page UI
├── contract_worker/
│   ├── workflow.py             # Temporal workflow definition
│   ├── activities.py           # Activities (email + Slack webhook notifications)
│   ├── gemini_analyzer.py      # Mock analyzer (deterministic)
│   └── postgres_db.py          # Postgres-backed state store (used by running stack)
├── tests/
│   ├── test_activities.py      # Unit tests — pure helper logic
│   ├── test_api_server.py      # Unit tests — API endpoints (stubbed deps)
│   └── test_contractiq_unit.py # Unit tests — analyzer + enums
├── requirements.txt
└── docker-compose.yml
```

### Note : In some places, we have used UUID/random generators solely for PoC purposes.