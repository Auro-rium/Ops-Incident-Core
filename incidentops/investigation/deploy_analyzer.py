from __future__ import annotations

import re

SYNC_RE = re.compile(r"\b(sync|blocking|sleep|requests\.|httpx\.|urllib)\b", re.IGNORECASE)


def analyze_deploy_diff(evidence: list[dict], deploy_hash: str | None) -> dict:
    findings = {
        "deploy_hash": deploy_hash,
        "changed_files": [],
        "summary_points": [],
        "diff_count": 0,
    }
    for item in evidence:
        if item.get("source_type") != "deploy":
            continue
        if deploy_hash and deploy_hash not in item.get("text", "").lower() and deploy_hash not in item.get("document_path", "").lower():
            continue
        findings["diff_count"] += 1
        if item.get("section_title"):
            findings["changed_files"].append(item["section_title"])
        if SYNC_RE.search(item.get("text", "")):
            findings["summary_points"].append("deploy diff suggests blocking or synchronous behavior changed")
    findings["changed_files"] = sorted(set(findings["changed_files"]))
    return findings
