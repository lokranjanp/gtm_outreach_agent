"""Reversible per-run signal exclusions; source documents remain available as evidence."""

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from outreach.models import now


def fingerprints(signal: dict) -> set[str]:
    def normalized(value):
        return re.sub(r"\s+", " ", value or "").strip().casefold()

    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    parts = urlsplit(signal.get("source_url", ""))
    source = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )
    claim, quote = normalized(signal.get("claim")), normalized(signal.get("quote"))
    return {
        "id:" + signal["id"],
        "claim:" + digest(claim),
        "quote:" + digest(quote),
        "source_claim:" + digest(source + claim),
    }


def active_evidence(evidence: list[dict], removed: list[dict]) -> list[dict]:
    excluded = (
        set().union(*(fingerprints(item["signal"]) for item in removed)) if removed else set()
    )
    return [item for item in evidence if not fingerprints(item) & excluded]


def edit_signals(state: dict, action: str, ids: list[str]) -> dict:
    removed = list(state.get("removed_signals", []))
    evidence = list(state.get("evidence", []))
    ids = set(ids)
    if action == "remove":
        known = {item["signal"]["id"] for item in removed}
        removed.extend(
            {"signal": item, "removed_at": now()}
            for item in evidence
            if item["id"] in ids and item["id"] not in known
        )
    elif action == "restore":
        evidence.extend(item["signal"] for item in removed if item["signal"]["id"] in ids)
        removed = [item for item in removed if item["signal"]["id"] not in ids]
    evidence = active_evidence(list({e["id"]: e for e in evidence}.values()), removed)
    active_ids = {e["id"] for e in evidence}
    used_ids = {s["evidence_id"] for s in state.get("selected", [])}
    used_ids.update(state.get("draft", {}).get("evidence_ids", []))
    invalidated = bool(used_ids - active_ids)
    versions = []
    for version in state.get("versions", []):
        version = dict(version)
        if set(version["draft"].get("evidence_ids", [])) - active_ids:
            version["outdated"] = True
        versions.append(version)
    updates = {
        "evidence": evidence,
        "removed_signals": removed,
        "versions": versions,
        "review_error": "",
        "requested_ids": [],
        "confirming_selection": False,
    }
    if invalidated:
        updates.update(
            draft={},
            selected=[],
            pending_approval=False,
            quality={},
            quality_issues=[],
            quality_warnings=[],
            repair_count=0,
            status="running",
        )
    return updates


def recipient_warnings(fit: dict | None) -> list[str]:
    if not fit:
        return []
    warnings = list(fit.get("warnings", []))
    if fit.get("recipient_fit") != "aligned":
        reason = (
            fit.get("explanation") or "The recipient may not own the relevant purchasing decision."
        )
        warnings.append("Recipient fit: " + reason)
    return list(dict.fromkeys(warnings))
