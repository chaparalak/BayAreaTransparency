import json
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import pandas as pd
from supabase import Client, create_client

DATA_DIR = Path("data/bay_area_transparency")
BATCH_SIZE = 1000

PRICE_CANDIDATE_COLUMNS = [
    "price",
    "standard_charge",
    "standard_charge|gross",
    "standard_charge|discounted_cash",
    "standard_charge|negotiated_dollar",
    "gross_charge",
    "negotiated_rate",
    "cash_price",
    "minimum",
    "maximum",
    "median_amount",
    "amount",
    "cost",
    "rate",
]

PROVIDER_CANDIDATE_COLUMNS = ["provider", "provider_name", "hospital", "facility", "organization", "name"]
ITEM_CANDIDATE_COLUMNS = ["item", "description", "service", "procedure", "drg", "billing_code", "code"]
LAT_CANDIDATE_COLUMNS = ["lat", "latitude", "y"]
LON_CANDIDATE_COLUMNS = ["lon", "lng", "long", "longitude", "x"]


def get_config() -> Tuple[Optional[str], Optional[str]]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    secrets_path = Path(".streamlit/secrets.toml")
    if secrets_path.exists():
        content = secrets_path.read_text(encoding="utf-8")
        if not url:
            for line in content.splitlines():
                if line.strip().startswith("SUPABASE_URL") and '"' in line:
                    url = line.split('"', 2)[1].strip()
        if not key:
            for line in content.splitlines():
                if line.strip().startswith("SUPABASE_SERVICE_ROLE_KEY") and '"' in line:
                    key = line.split('"', 2)[1].strip()
    return url, key


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).replace("\ufeff", "").strip().lower() for c in out.columns]
    return out


def find_column(columns: Iterable[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in columns:
            return c
    return None


def parse_price(value) -> Optional[float]:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def flatten_json_records(obj) -> List[dict]:
    records = []

    def walk(node, context=None):
        if context is None:
            context = {}
        if isinstance(node, dict):
            scalar_fields = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
            next_context = {**context, **scalar_fields}
            children = [v for v in node.values() if isinstance(v, (dict, list))]
            if not children:
                records.append(next_context)
                return
            for child in children:
                walk(child, next_context)
        elif isinstance(node, list):
            for item in node:
                walk(item, context)

    walk(obj)
    return records


def read_source_file(file_path: Path) -> pd.DataFrame:
    filename = file_path.name.lower()
    if filename.endswith(".csv"):
        raw = pd.read_csv(file_path, header=None, dtype=str, engine="python", on_bad_lines="skip")
        if len(raw) > 3 and str(raw.iloc[2, 0]).strip().lower() in {"description", "item", "service"}:
            header = raw.iloc[2].fillna("").astype(str).tolist()
            body = raw.iloc[3:].copy()
            body.columns = header
            body = body.reset_index(drop=True)
            hospital_name = raw.iloc[1, 0] if len(raw) > 1 else None
            if hospital_name:
                body["hospital_name"] = str(hospital_name)
            return body
        return pd.read_csv(file_path, dtype=str, engine="python", on_bad_lines="skip")

    if filename.endswith(".json"):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except JSONDecodeError as exc:
            raise ValueError(f"Invalid/incomplete JSON file: {exc}")
        if isinstance(payload, list):
            return pd.json_normalize(payload)
        return pd.DataFrame(flatten_json_records(payload))

    raise ValueError(f"Unsupported file type: {file_path.name}")


def extract_charge_rows(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    provider_col = find_column(df.columns, PROVIDER_CANDIDATE_COLUMNS)
    price_col = find_column(df.columns, PRICE_CANDIDATE_COLUMNS)
    item_col = find_column(df.columns, ITEM_CANDIDATE_COLUMNS)
    lat_col = find_column(df.columns, LAT_CANDIDATE_COLUMNS)
    lon_col = find_column(df.columns, LON_CANDIDATE_COLUMNS)

    if provider_col is None:
        provider_col = find_column(df.columns, ["hospital_name", "location_name"]) or "_source_file"
    if price_col is None:
        return pd.DataFrame(columns=["provider", "item", "price", "lat", "lon", "source_file"])

    work = df.copy()
    work["_provider"] = work[provider_col].astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "none": pd.NA})
    work["_price"] = work[price_col].apply(parse_price)
    work["_item"] = work[item_col].astype(str).str.strip() if item_col else ""
    work["_lat"] = pd.to_numeric(work[lat_col], errors="coerce") if lat_col else None
    work["_lon"] = pd.to_numeric(work[lon_col], errors="coerce") if lon_col else None
    work = work.dropna(subset=["_provider", "_price"])
    if work.empty:
        return pd.DataFrame(columns=["provider", "item", "price", "lat", "lon", "source_file"])

    return pd.DataFrame(
        {
            "provider": work["_provider"],
            "item": work["_item"],
            "price": work["_price"],
            "lat": work["_lat"],
            "lon": work["_lon"],
            "source_file": source_name,
        }
    )


def upsert_batches(client: Client, rows: List[dict]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        client.table("charges").insert(batch).execute()


def clear_table(client: Client) -> None:
    client.table("charges").delete().neq("provider", "__never__match__").execute()


def main():
    url, key = get_config()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required in env or .streamlit/secrets.toml")

    client = create_client(url, key)
    files = sorted([*DATA_DIR.glob("*.csv"), *DATA_DIR.glob("*.json")])
    if not files:
        raise RuntimeError(f"No files found in {DATA_DIR}")

    clear_table(client)
    total_rows = 0
    for file_path in files:
        try:
            raw = normalize_columns(read_source_file(file_path))
            raw["_source_file"] = file_path.name
            rows = extract_charge_rows(raw, file_path.name)
            if rows.empty:
                print(f"Skipped (no usable rows): {file_path.name}")
                continue
            payload = rows.where(pd.notnull(rows), None).to_dict(orient="records")
            upsert_batches(client, payload)
            total_rows += len(payload)
            print(f"Loaded {len(payload):,} rows from {file_path.name}")
        except Exception as exc:
            print(f"Error in {file_path.name}: {exc}")

    print(f"Done. Total rows loaded: {total_rows:,}")


if __name__ == "__main__":
    main()
