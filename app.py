import json
import os
import re
from difflib import get_close_matches
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import pydeck as pdk
import streamlit as st
from supabase import create_client

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


LOCAL_SEARCH_SYNONYMS = {
    "mri": ["magnetic resonance imaging", "mr", "mr imaging"],
    "ct": ["cat scan", "computed tomography"],
    "xray": ["x-ray", "radiograph"],
    "ultrasound": ["sonogram", "us"],
    "ekg": ["ecg", "electrocardiogram"],
    "er": ["emergency", "emergency department", "ed"],
    "knee replacement": ["arthroplasty knee", "total knee arthroplasty", "tka"],
    "hip replacement": ["arthroplasty hip", "total hip arthroplasty", "tha"],
    "c section": ["cesarean", "cesarean section", "c-section"],
    "colonoscopy": ["colonoscopy diagnostic", "colon screening"],
}


st.set_page_config(page_title="Bay Area Price Transparency Mapper", layout="wide")

DATA_DIR = Path("data/bay_area_transparency")


BAY_AREA_PROVIDER_COORDS: Dict[str, Tuple[float, float]] = {
    "ucsf": (37.7631, -122.4586),
    "ucsf health": (37.7631, -122.4586),
    "stanford": (37.4336, -122.1750),
    "stanford health care": (37.4336, -122.1750),
    "kaiser": (37.7835, -122.4060),
    "sutter": (37.7912, -122.4032),
    "john muir": (37.9058, -122.0679),
    "washington hospital": (37.5572, -121.9807),
    "el camino": (37.3688, -122.0795),
    "northbay": (38.2494, -122.0400),
    "northbay medical center": (38.2494, -122.0400),
    "vacavalley": (38.3675, -121.9689),
    "vacaville": (38.3566, -121.9877),
    "fairfield": (38.2494, -122.0400),
    "dignity": (37.7749, -122.4194),
    "sequoia hospital": (37.4863, -122.2325),
    "redwood city": (37.4852, -122.2364),
    "dominican hospital": (36.9741, -122.0308),
    "santa cruz": (36.9741, -122.0308),
    "mercy general hospital": (38.5688, -121.4424),
    "mercy hospital of folsom": (38.6699, -121.1661),
    "mercy san juan medical center": (38.6613, -121.3470),
    "methodist hospital of sacramento": (38.4788, -121.4337),
    "woodland memorial hospital": (38.6785, -121.7733),
    "sacramento": (38.5816, -121.4944),
    "folsom": (38.6779, -121.1761),
    "woodland": (38.6785, -121.7733),
    "saint francis memorial hospital": (37.7898, -122.4167),
    "st. mary's medical center": (37.7749, -122.4460),
    "st mary": (37.7749, -122.4460),
    "stanyan": (37.7749, -122.4460),
    "hyde": (37.7898, -122.4167),
    "good samaritan": (37.2516, -121.9499),
    "alta bates": (37.8208, -122.2625),
    "marinhealth": (37.9485, -122.5268),
    "county": (37.6041, -122.3863),
    "oakland": (37.8044, -122.2711),
    "san francisco": (37.7749, -122.4194),
    "san jose": (37.3382, -121.8863),
    "palo alto": (37.4419, -122.1430),
    "berkeley": (37.8715, -122.2730),
    "fremont": (37.5485, -121.9886),
    "walnut creek": (37.9101, -122.0652),
}


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

PROVIDER_CANDIDATE_COLUMNS = [
    "provider",
    "provider_name",
    "hospital",
    "facility",
    "organization",
    "name",
]

ITEM_CANDIDATE_COLUMNS = [
    "item",
    "description",
    "service",
    "procedure",
    "drg",
    "billing_code",
    "code",
]

LAT_CANDIDATE_COLUMNS = ["lat", "latitude", "y"]
LON_CANDIDATE_COLUMNS = ["lon", "lng", "long", "longitude", "x"]


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).replace("\ufeff", "").strip().lower() for c in out.columns]
    return out


def find_column(columns: Iterable[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in columns:
            return c
    return None


def flatten_json_records(obj) -> List[dict]:
    records = []

    def walk(node, context=None):
        if context is None:
            context = {}
        if isinstance(node, dict):
            scalar_fields = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
            next_context = {**context, **scalar_fields}
            child_containers = [v for v in node.values() if isinstance(v, (dict, list))]
            if not child_containers:
                records.append(next_context)
                return
            for child in child_containers:
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
        # CMS-style CSVs often include two metadata rows before the true header.
        if len(raw) > 3 and str(raw.iloc[2, 0]).strip().lower() in {"description", "item", "service"}:
            header = raw.iloc[2].fillna("").astype(str).tolist()
            body = raw.iloc[3:].copy()
            body.columns = header
            body = body.reset_index(drop=True)

            hospital_name = None
            if len(raw) > 1:
                hospital_name = raw.iloc[1, 0]
            if hospital_name:
                body["hospital_name"] = str(hospital_name)
            return body

        # Fallback for standard single-header CSV files.
        return pd.read_csv(file_path, dtype=str, engine="python", on_bad_lines="skip")
    if filename.endswith(".json"):
        try:
            with file_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except JSONDecodeError as exc:
            raise ValueError(
                "Invalid/incomplete JSON file. Re-download this file and retry. "
                f"Parser details: {exc}"
            )
        if isinstance(payload, list):
            return pd.json_normalize(payload)
        flattened = flatten_json_records(payload)
        return pd.DataFrame(flattened)
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
    work["_provider"] = work[provider_col].astype(str).str.strip()
    work["_provider"] = work["_provider"].replace({"": pd.NA, "nan": pd.NA, "none": pd.NA})
    work["_price"] = work[price_col].apply(parse_price)
    work["_item"] = work[item_col].astype(str).str.strip() if item_col else ""
    work["_lat"] = pd.to_numeric(work[lat_col], errors="coerce") if lat_col else None
    work["_lon"] = pd.to_numeric(work[lon_col], errors="coerce") if lon_col else None
    work = work.dropna(subset=["_provider", "_price"])
    if work.empty:
        return pd.DataFrame(columns=["provider", "item", "price", "lat", "lon", "source_file"])

    out = pd.DataFrame(
        {
            "provider": work["_provider"],
            "item": work["_item"],
            "price": work["_price"],
            "lat": work["_lat"],
            "lon": work["_lon"],
            "source_file": source_name,
        }
    )
    return out


def get_supabase_config() -> Tuple[Optional[str], Optional[str]]:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY")
    return url, key


@st.cache_resource
def init_supabase_client():
    url, key = get_supabase_config()
    if not url or not key:
        return None
    return create_client(url, key)


def read_db_data(client) -> pd.DataFrame:
    all_rows = []
    page_size = 1000
    start = 0
    while True:
        resp = (
            client.table("charges")
            .select("provider,item,price,lat,lon,source_file")
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return pd.DataFrame(all_rows)


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


def infer_coords(provider_name: str) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(provider_name, str):
        return None, None
    n = provider_name.lower()
    for key, coords in BAY_AREA_PROVIDER_COORDS.items():
        if key in n:
            return coords
    return None, None


def color_from_rank(rank_pct: float) -> List[int]:
    # Low price -> green, high price -> red
    r = int(255 * rank_pct)
    g = int(200 * (1 - rank_pct) + 40)
    b = 70
    return [r, g, b, 170]


def expand_query_terms_llm(query: str) -> List[str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return [query]

    client = OpenAI(api_key=api_key)
    prompt = (
        "You expand healthcare billing search terms for hospital price files. "
        "Return only a comma-separated list of up to 10 short search terms. "
        "Include procedure names, common abbreviations, and billing-style variants. "
        f"User query: {query}"
    )
    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            max_output_tokens=120,
        )
        text = (resp.output_text or "").strip()
        terms = [t.strip() for t in text.split(",") if t.strip()]
        if query not in terms:
            terms.insert(0, query)
        return terms[:10]
    except Exception:
        return [query]


def expand_query_terms_local(query: str, item_series: Optional[pd.Series] = None) -> List[str]:
    q = query.strip().lower()
    terms: List[str] = [q]

    for key, vals in LOCAL_SEARCH_SYNONYMS.items():
        if key in q or q in key:
            terms.extend(vals)

    for token in re.split(r"[^a-z0-9]+", q):
        if token in LOCAL_SEARCH_SYNONYMS:
            terms.extend(LOCAL_SEARCH_SYNONYMS[token])

    code_match = re.findall(r"\b(?:drg|cpt|hcpcs)?\s*-?\s*([a-z]?\d{3,5}[a-z]?)\b", q)
    terms.extend(code_match)

    if item_series is not None and not item_series.empty:
        sample = (
            item_series.astype(str)
            .str.lower()
            .dropna()
            .drop_duplicates()
            .head(5000)
            .tolist()
        )
        close = get_close_matches(q, sample, n=6, cutoff=0.72)
        terms.extend(close)

    deduped = []
    seen = set()
    for t in terms:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:20]


def build_provider_summary(df: pd.DataFrame, mode: str, item_query: str, use_llm_search: bool = False) -> pd.DataFrame:
    provider_col = find_column(df.columns, PROVIDER_CANDIDATE_COLUMNS)
    price_col = find_column(df.columns, PRICE_CANDIDATE_COLUMNS)
    item_col = find_column(df.columns, ITEM_CANDIDATE_COLUMNS)
    lat_col = find_column(df.columns, LAT_CANDIDATE_COLUMNS)
    lon_col = find_column(df.columns, LON_CANDIDATE_COLUMNS)

    if provider_col is None:
        fallback_provider = find_column(df.columns, ["hospital_name", "location_name", "_source_file"])
        if fallback_provider is not None:
            provider_col = fallback_provider

    if provider_col is None or price_col is None:
        raise ValueError(
            "Could not identify required columns. Need provider and price columns (for example: provider_name + standard_charge)."
        )

    work = df.copy()
    work["_provider"] = work[provider_col].astype(str).str.strip()
    work["_provider"] = work["_provider"].replace({"": pd.NA, "nan": pd.NA, "none": pd.NA})
    work["_price"] = work[price_col].apply(parse_price)
    work = work.dropna(subset=["_provider", "_price"])

    if mode == "Specific item" and item_col is not None and item_query.strip():
        q = item_query.strip()
        if use_llm_search:
            llm_terms = expand_query_terms_llm(q)
            if llm_terms == [q]:
                terms = expand_query_terms_local(q, work[item_col])
            else:
                terms = llm_terms
        else:
            terms = expand_query_terms_local(q, work[item_col])
        pattern_parts = [re.escape(str(t).lower()) for t in terms if str(t).strip()]
        if not pattern_parts:
            return pd.DataFrame()
        pattern = "|".join(pattern_parts)
        work = work[work[item_col].astype(str).str.lower().str.contains(pattern, na=False, regex=True)]

    if work.empty:
        return pd.DataFrame()

    grouped = (
        work.groupby("_provider", as_index=False)["_price"]
        .mean()
        .rename(columns={"_provider": "provider", "_price": "avg_price"})
    )

    if lat_col and lon_col:
        coords = (
            work[["_provider", lat_col, lon_col]]
            .dropna()
            .drop_duplicates(subset=["_provider"])
            .rename(columns={"_provider": "provider", lat_col: "lat", lon_col: "lon"})
        )
    else:
        coords = pd.DataFrame(columns=["provider", "lat", "lon"])

    merged = grouped.merge(coords, on="provider", how="left")

    inferred = merged["provider"].apply(infer_coords)
    merged["lat_infer"] = inferred.apply(lambda p: p[0])
    merged["lon_infer"] = inferred.apply(lambda p: p[1])
    merged["lat"] = merged["lat"].fillna(merged["lat_infer"])
    merged["lon"] = merged["lon"].fillna(merged["lon_infer"])
    merged = merged.dropna(subset=["lat", "lon"])

    if merged.empty:
        return merged

    merged["rank_pct"] = merged["avg_price"].rank(method="min", pct=True)
    merged["color"] = merged["rank_pct"].apply(color_from_rank)
    merged["radius"] = 1400
    return merged[["provider", "avg_price", "lat", "lon", "rank_pct", "color", "radius"]]


st.title("Bay Area Hospital & Provider Price Transparency Map")
st.write(
    "Data is loaded from the local transparency database built from files in data/bay_area_transparency. "
    "The map colors providers by lower (green) vs higher (red) average prices."
)

with st.sidebar:
    st.header("Controls")
    mode = st.radio("View mode", ["Average across all items", "Specific item"])
    item_query = ""
    if mode == "Specific item":
        item_query = st.text_input("Search item/procedure", placeholder="e.g. MRI, CT abdomen, DRG 470")
        use_llm_search = st.toggle("Smart search (LLM if key exists)", value=False)
    else:
        use_llm_search = False
    st.caption("Use supabase_loader.py to ingest local files into Supabase.")

client = init_supabase_client()
if client is None:
    st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in .streamlit/secrets.toml or environment variables.")
    st.stop()

try:
    all_data = read_db_data(client)
except Exception as exc:
    st.error(f"Unable to query Supabase charges table via API: {exc}")
    st.stop()

if all_data.empty:
    st.error("No records found in Supabase charges table. Run supabase_loader.py to ingest data.")
    st.stop()

st.caption(f"Database: Supabase API | records: {len(all_data):,}")

summary = build_provider_summary(all_data, mode, item_query, use_llm_search=use_llm_search)

if summary.empty:
    st.error(
        "No mappable provider records found for the selected mode/filter. "
        "Try a broader item query or include provider latitude/longitude columns."
    )
    st.stop()

low = float(summary["avg_price"].min())
high = float(summary["avg_price"].max())
st.metric("Providers mapped", len(summary))
st.caption(f"Price range used for coloring: ${low:,.2f} to ${high:,.2f}")

view_state = pdk.ViewState(
    latitude=float(summary["lat"].mean()),
    longitude=float(summary["lon"].mean()),
    zoom=8.2,
    pitch=25,
)

layer = pdk.Layer(
    "ScatterplotLayer",
    data=summary,
    get_position="[lon, lat]",
    get_fill_color="color",
    get_radius="radius",
    pickable=True,
)

tooltip = {
    "html": "<b>{provider}</b><br/>Avg price: <b>${avg_price}</b>",
    "style": {"backgroundColor": "#111827", "color": "white"},
}

st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view_state, tooltip=tooltip))

st.subheader("Provider price table")
st.dataframe(summary.sort_values("avg_price", ascending=True), use_container_width=True)
