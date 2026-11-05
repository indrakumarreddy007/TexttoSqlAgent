"""Development identity store (config/users.yaml) and human-readable access scopes.

Stands in for an OIDC/JWT identity provider; shared by the API and the Streamlit app.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from sqlagent.config import ROOT
from sqlagent.core.schemas import UserContext

USERS_PATH = ROOT / "config" / "users.yaml"

ROLE_LABELS = {"exec": "Executive", "regional_mgr": "Regional manager", "analyst": "Business analyst",
               "hr_analyst": "HR analyst", "admin": "Data steward"}
_SCOPES = {"exec": "all regions · customer PII masked", "analyst": "all regions · no PII",
           "hr_analyst": "HR only · names masked · pay aggregated (min 5)", "admin": "unrestricted"}


@lru_cache
def load_dev_users(path: Path = USERS_PATH) -> dict[str, UserContext]:
    doc = yaml.safe_load(path.read_text())
    return {tok: UserContext(**u) for tok, u in doc["tokens"].items()}


def describe(u: UserContext) -> tuple[str, str]:
    """(role label, access scope) for display."""
    if u.role == "regional_mgr":
        scope = f"regions {', '.join(u.regions) or 'none'} · no PII"
    else:
        scope = _SCOPES.get(u.role, "")
    return ROLE_LABELS.get(u.role, u.role), scope
