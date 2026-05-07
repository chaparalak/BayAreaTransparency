# Bay Area Price Transparency Mapper

Interactive Streamlit app for exploring hospital/provider price transparency data on a map, backed by Supabase via REST API.

## What changed

This project now uses an online database (Supabase) instead of local SQLite for primary storage/querying. Large source files are ingested once, then queried efficiently by the app.

## Project structure

```text
MapProj/
  app.py
  supabase_loader.py
  requirements.txt
  README.md
  .streamlit/
    secrets.toml.example
  data/
    bay_area_transparency/
      *.csv
      *.json
```

## Prerequisites

- Python 3.10+
- A Supabase project
- Supabase project URL and API keys

## Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Configure Supabase credentials.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Credentials file

Copy the template and fill in values:

```bash
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Set:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY` (used by the Streamlit app)
- `SUPABASE_SERVICE_ROLE_KEY` (used by loader to write data)

You can also set these as environment variables instead of using secrets.

## Create table in Supabase (one-time)

In Supabase SQL Editor, run:

```sql
create table if not exists charges (
  id bigint generated always as identity primary key,
  provider text not null,
  item text,
  price double precision,
  lat double precision,
  lon double precision,
  source_file text,
  inserted_at timestamptz default now()
);
```

## Ingest data into Supabase

Place source files in `data/bay_area_transparency`, then run:

```bash
python supabase_loader.py
```

What loader does:

- Truncates existing `charges` data
- Parses all `*.csv` and `*.json` files
- Batch inserts normalized rows into Supabase via API

## Run the app

```bash
streamlit run app.py
```

App behavior:

- Reads from Supabase `charges` table
- Supports:
  - **Average across all items**
  - **Specific item** search
- Uses local synonym/fuzzy expansion
- Optionally uses LLM term expansion when `OPENAI_API_KEY` is set

## Data model

`charges` table columns:

- `id` (bigserial primary key)
- `provider` (text)
- `item` (text)
- `price` (double precision)
- `lat` (double precision)
- `lon` (double precision)
- `source_file` (text)
- `inserted_at` (timestamptz)

## Troubleshooting

- **Missing SUPABASE_URL / keys**
  - Add them in `.streamlit/secrets.toml` or environment variables
- **No records in app**
  - Run `python supabase_loader.py` and confirm loader output
- **Connection refused / timeout**
  - Check network/VPN/proxy and Supabase project status
- **Large ingest is slow**
  - Keep batch size default, run from stable network, retry failed files

## GitHub publish (first time)

```bash
git init
git rm -r --cached data/bay_area_transparency
git add .
git commit -m "Initial commit: Bay Area price transparency mapper with Supabase"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

`data/bay_area_transparency/` is ignored in `.gitignore`, so raw source files stay local.

## Security notes

- Never commit `.streamlit/secrets.toml`
- Keep `SUPABASE_SERVICE_ROLE_KEY` private
- If rotating credentials, update secrets and rerun app/loader
