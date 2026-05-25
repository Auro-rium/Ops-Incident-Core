from __future__ import annotations

from dataclasses import dataclass, field
import re


INTENT_CODE_LOCATION = "code_location"
INTENT_CONFIG_API_DOC = "config_api_doc"
INTENT_ARCHITECTURE_DOCS = "architecture_docs"
INTENT_RUNTIME_LOGS = "runtime_logs"
INTENT_DEPLOY_CHANGE = "deploy_change"
INTENT_INCIDENT_HISTORY = "incident_history"
INTENT_ROOT_CAUSE = "root_cause_investigation"

_CONFIG_TERMS = {
    "config",
    "configuration",
    "env",
    "environment",
    "yaml",
    "yml",
    "json",
    "toml",
    "ini",
    "docker",
    "compose",
    "helm",
    "kustomize",
    "openapi",
    "swagger",
    "endpoint",
    "endpoints",
}
_CODE_TERMS = {
    "implemented",
    "implementation",
    "defined",
    "definition",
    "handler",
    "handlers",
    "function",
    "functions",
    "class",
    "classes",
    "method",
    "methods",
    "module",
    "package",
    "source",
    "code",
}
_ARCHITECTURE_TERMS = {
    "architecture",
    "design",
    "diagram",
    "overview",
    "boundary",
    "boundaries",
    "component",
    "components",
    "how does",
    "how is",
    "service map",
}
_RUNTIME_TERMS = {
    "logs",
    "log",
    "trace",
    "traces",
    "request id",
    "trace id",
    "error",
    "errors",
    "timeout",
    "timeouts",
    "runtime",
    "symptom",
    "symptoms",
}
_DEPLOY_TERMS = {
    "deploy",
    "deployed",
    "release",
    "rollback",
    "diff",
    "patch",
    "commit",
    "revision",
    "what changed",
    "changed before",
}
_INCIDENT_HISTORY_TERMS = {
    "happened before",
    "similar incident",
    "previous incident",
    "postmortem",
    "postmortems",
    "seen this before",
}
_ROOT_CAUSE_TERMS = {
    "why did",
    "root cause",
    "relevant to investigating",
    "investigating",
    "investigate",
    "caused by",
    "cause of",
    "latency",
    "spike",
    "slow",
    "slower",
    "slowdown",
    "outage",
}
_TOKEN_RE = re.compile(r"[a-z0-9_./-]+")


@dataclass(slots=True)
class QueryIntent:
    intent: str
    secondary_intents: list[str] = field(default_factory=list)
    preferred_source_types: list[str] = field(default_factory=list)
    preferred_chunk_types: list[str] = field(default_factory=list)
    query_terms: list[str] = field(default_factory=list)
    explanation: str = ""

    @property
    def is_incident_like(self) -> bool:
        return self.intent in {
            INTENT_RUNTIME_LOGS,
            INTENT_DEPLOY_CHANGE,
            INTENT_INCIDENT_HISTORY,
            INTENT_ROOT_CAUSE,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "secondary_intents": list(self.secondary_intents),
            "preferred_source_types": list(self.preferred_source_types),
            "preferred_chunk_types": list(self.preferred_chunk_types),
            "query_terms": list(self.query_terms),
            "explanation": self.explanation,
        }


def classify_query_intent(query: str) -> QueryIntent:
    lower = query.lower().strip()
    query_terms = [token for token in _TOKEN_RE.findall(lower) if len(token) >= 3][:30]

    if _contains_any(lower, _INCIDENT_HISTORY_TERMS):
        return QueryIntent(
            intent=INTENT_INCIDENT_HISTORY,
            preferred_source_types=["incident", "runbook", "logs"],
            preferred_chunk_types=["incident_section", "markdown_section", "log_window"],
            query_terms=query_terms,
            explanation="query asks for previous incidents or historical comparisons",
        )
    if _contains_any(lower, _ROOT_CAUSE_TERMS):
        return QueryIntent(
            intent=INTENT_ROOT_CAUSE,
            secondary_intents=[
                intent
                for intent, terms in (
                    (INTENT_RUNTIME_LOGS, _RUNTIME_TERMS),
                    (INTENT_DEPLOY_CHANGE, _DEPLOY_TERMS),
                    (INTENT_INCIDENT_HISTORY, _INCIDENT_HISTORY_TERMS),
                )
                if _contains_any(lower, terms)
            ],
            preferred_source_types=["logs", "deploy", "incident", "runbook", "code"],
            preferred_chunk_types=["log_window", "deploy_diff", "incident_section", "function", "markdown_section"],
            query_terms=query_terms,
            explanation="query asks for incident explanation, causality, or investigation support",
        )
    if _contains_any(lower, _DEPLOY_TERMS) and not _contains_any(lower, {"where is", "where are"}):
        return QueryIntent(
            intent=INTENT_DEPLOY_CHANGE,
            preferred_source_types=["deploy", "logs", "incident", "code"],
            preferred_chunk_types=["deploy_diff", "release_note", "log_window", "function", "config_section"],
            query_terms=query_terms,
            explanation="query asks about deploys, releases, diffs, or changes before an incident",
        )
    if _contains_any(lower, _RUNTIME_TERMS):
        return QueryIntent(
            intent=INTENT_RUNTIME_LOGS,
            preferred_source_types=["logs", "runbook", "deploy", "incident"],
            preferred_chunk_types=["log_window", "error_cluster", "markdown_section", "deploy_diff"],
            query_terms=query_terms,
            explanation="query asks for runtime symptoms, errors, traces, or timeouts",
        )
    if _contains_any(lower, _CONFIG_TERMS):
        return QueryIntent(
            intent=INTENT_CONFIG_API_DOC,
            secondary_intents=[INTENT_CODE_LOCATION] if "implemented" in lower else [],
            preferred_source_types=["config", "api_doc", "code", "runbook"],
            preferred_chunk_types=["config_section", "api_endpoint", "proto_service", "function", "markdown_section"],
            query_terms=query_terms,
            explanation="query asks about configuration, API definitions, or endpoint documentation",
        )
    if _contains_any(lower, _ARCHITECTURE_TERMS):
        return QueryIntent(
            intent=INTENT_ARCHITECTURE_DOCS,
            preferred_source_types=["runbook", "api_doc", "code", "config"],
            preferred_chunk_types=["markdown_section", "module", "proto_service", "config_section"],
            query_terms=query_terms,
            explanation="query asks for architecture, boundaries, or design context",
        )
    if _looks_like_code_location(lower):
        return QueryIntent(
            intent=INTENT_CODE_LOCATION,
            preferred_source_types=["code", "api_doc", "config", "runbook"],
            preferred_chunk_types=["function", "class", "module", "code_file", "api_endpoint", "proto_service"],
            query_terms=query_terms,
            explanation="query asks where behavior, modules, or symbols are implemented",
        )
    return QueryIntent(
        intent=INTENT_ARCHITECTURE_DOCS,
        preferred_source_types=["runbook", "code", "config", "api_doc"],
        preferred_chunk_types=["markdown_section", "module", "function", "config_section"],
        query_terms=query_terms,
        explanation="default to architecture/docs retrieval when intent is ambiguous",
    )


def investigate_supported(intent: QueryIntent, available_source_types: set[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if intent.intent in {INTENT_CODE_LOCATION, INTENT_CONFIG_API_DOC, INTENT_ARCHITECTURE_DOCS}:
        reasons.append("query intent is repo lookup rather than runtime incident investigation")
        return False, reasons
    if intent.intent == INTENT_RUNTIME_LOGS and "logs" not in available_source_types:
        reasons.append("runtime investigation requires logs")
        return False, reasons
    if intent.intent == INTENT_DEPLOY_CHANGE and "deploy" not in available_source_types:
        reasons.append("deploy analysis requires deploy history or diffs")
        return False, reasons
    if intent.intent == INTENT_INCIDENT_HISTORY and "incident" not in available_source_types:
        reasons.append("incident history lookup requires incident reports or postmortems")
        return False, reasons
    if intent.intent == INTENT_ROOT_CAUSE:
        if "logs" not in available_source_types and "deploy" not in available_source_types:
            reasons.append("root-cause investigation requires logs or deploy/change evidence")
            return False, reasons
    return True, reasons


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _looks_like_code_location(text: str) -> bool:
    if _contains_any(text, _CODE_TERMS):
        return True
    return any(
        phrase in text
        for phrase in (
            "where is",
            "where are",
            "which file",
            "which files",
            "what file",
            "what files",
        )
    )
