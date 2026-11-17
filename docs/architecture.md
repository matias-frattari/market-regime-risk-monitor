# Architecture

> TODO: full architecture write-up (pipeline diagram, modules, scheduling).

## Runtime dependencies

| Library | Why |
|---|---|
| `duckdb` | Embedded analytical warehouse; single file, no server, fast SQL over local data |
| `pyyaml` | Read the editable config files (`config/*.yaml`) |
| `python-dotenv` | Load API keys from the gitignored `.env` (used by the ingestion CLI) |
| `requests` | HTTP client for the Twelve Data and FRED APIs (ingestion) |

Dev: `ruff` (lint/format), `pytest` (tests), `hatchling` (build backend).
