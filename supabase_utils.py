import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import streamlit as st
from supabase import create_client


def parse_simple_toml_secrets(file_path: Path) -> Dict[str, str]:
    if not file_path.exists():
        return {}
    values: Dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            values[key] = val[1:-1]
    return values


def get_supabase_config_for_app() -> Tuple[Optional[str], Optional[str]]:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY")
    return url, key


def get_supabase_config_for_loader() -> Tuple[Optional[str], Optional[str]]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    parsed = parse_simple_toml_secrets(Path(".streamlit/secrets.toml"))
    if not url:
        url = parsed.get("SUPABASE_URL")
    if not key:
        key = parsed.get("SUPABASE_SERVICE_ROLE_KEY")
    return url, key


def create_supabase_client(url: Optional[str], key: Optional[str]):
    if not url or not key:
        return None
    return create_client(url, key)
