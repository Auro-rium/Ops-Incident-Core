from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.retrieval.citation_builder import build_citations
from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import hybrid_search
from incidentops.retrieval.reranker import rerank_with_debug_async


async def search_evidence(db: AsyncSession, project_id: uuid.UUID, query: str, top_k: int, reranker_model: str, filters: dict | None = None) -> list[dict]:
    raw = await hybrid_search(db, project_id, query, top_k=max(top_k * 3, 30), filters=filters)
    ranked, _ = await rerank_with_debug_async(query, raw, model_name=reranker_model, top_k=top_k)
    evidence = pack_evidence(ranked, max_evidence=top_k)
    build_citations(evidence)
    return evidence


def get_deploy_diff(evidence: list[dict], deploy_hash: str | None) -> list[dict]:
    return [
        item for item in evidence
        if item.get("source_type") == "deploy"
        and (deploy_hash is None or deploy_hash in item.get("document_path", "") or deploy_hash in item.get("text", ""))
    ]


def get_logs_for_window(evidence: list[dict]) -> list[dict]:
    return [item for item in evidence if item.get("source_type") == "logs"]


def find_similar_incidents(evidence: list[dict]) -> list[dict]:
    return [item for item in evidence if item.get("source_type") == "incident"]


def draft_incident_report(final_answer: dict) -> dict:
    report = {
        "title": f"Incident Report: {final_answer.get('task_type', 'investigation')}",
        "markdown": "\n".join([
            "# Incident Report",
            "",
            f"## Summary\n{final_answer.get('likely_root_cause', {}).get('summary', '')}",
            "",
            f"## Suggested Fix\n{final_answer.get('suggested_fix', '')}",
        ]),
    }
    return report


def draft_github_issue(final_answer: dict) -> dict:
    root = final_answer.get("likely_root_cause", {})
    return {
        "title": f"Investigate remediation: {root.get('summary', 'incident follow-up')[:80]}",
        "body": "\n".join([
            "## Context",
            root.get("summary", ""),
            "",
            "## Suggested Fix",
            final_answer.get("suggested_fix", ""),
        ]),
    }


def request_approval(action_type: str) -> dict:
    return {"action_type": action_type, "requires_approval": True}
