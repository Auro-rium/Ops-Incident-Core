from __future__ import annotations

from dataclasses import dataclass, field
import re


INTENT_CODE_LOCATION = "code_location"
INTENT_ARCHITECTURE = "architecture"
INTENT_CONFIG_LOOKUP = "config_lookup"
INTENT_API_CONTRACT = "api_contract"
INTENT_RUNTIME_INCIDENT = "runtime_incident"
INTENT_DEPLOY_REGRESSION = "deploy_regression"
INTENT_PREVIOUS_INCIDENT = "previous_incident"
INTENT_RUNBOOK_LOOKUP = "runbook_lookup"
INTENT_GENERIC = "generic"

# Backward-compatible aliases for older callers/tests.
INTENT_CONFIG_API_DOC = INTENT_CONFIG_LOOKUP
INTENT_ARCHITECTURE_DOCS = INTENT_ARCHITECTURE
INTENT_RUNTIME_LOGS = INTENT_RUNTIME_INCIDENT
INTENT_DEPLOY_CHANGE = INTENT_DEPLOY_REGRESSION
INTENT_INCIDENT_HISTORY = INTENT_PREVIOUS_INCIDENT
INTENT_ROOT_CAUSE = INTENT_RUNTIME_INCIDENT

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
}
_API_TERMS = {
    "api",
    "apis",
    "openapi",
    "swagger",
    "endpoint",
    "endpoints",
    "route",
    "routes",
    "rpc",
    "proto",
    "protobuf",
    "contract",
    "schema",
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
    "latency",
    "spike",
    "slow",
    "slower",
    "slowdown",
    "outage",
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
_RUNBOOK_TERMS = {
    "runbook",
    "playbook",
    "remediation",
    "mitigation",
    "rollback steps",
    "fix steps",
    "operator guide",
}
_ROOT_CAUSE_TERMS = {
    "why did",
    "root cause",
    "relevant to investigating",
    "investigating",
    "investigate",
    "caused by",
    "cause of",
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
            INTENT_RUNTIME_INCIDENT,
            INTENT_DEPLOY_REGRESSION,
            INTENT_PREVIOUS_INCIDENT,
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
            intent=INTENT_PREVIOUS_INCIDENT,
            preferred_source_types=["incident", "runbook", "logs"],
            preferred_chunk_types=["incident_section", "incident_root_cause", "incident_timeline", "markdown_heading_section", "log_time_window"],
            query_terms=query_terms,
            explanation="query asks for previous incidents or historical comparisons",
        )
    if _contains_any(lower, _RUNBOOK_TERMS):
        return QueryIntent(
            intent=INTENT_RUNBOOK_LOOKUP,
            preferred_source_types=["runbook", "incident", "deploy", "config"],
            preferred_chunk_types=["markdown_procedure", "markdown_heading_section", "incident_action", "deploy_commit", "deploy_diff", "config_section"],
            query_terms=query_terms,
            explanation="query asks for runbooks, remediation, or operational procedures",
        )
    if _looks_like_code_location(lower):
        return QueryIntent(
            intent=INTENT_CODE_LOCATION,
            preferred_source_types=["code", "api_doc", "config"],
            preferred_chunk_types=[
                "go_function",
                "go_method",
                "go_type",
                "go_struct",
                "go_interface",
                "python_function",
                "python_class",
                "ts_function",
                "ts_class",
                "ts_interface",
                "java_method",
                "java_class",
                "java_interface",
                "function",
                "class",
                "module",
                "go_module",
                "code_file",
                "openapi_endpoint",
                "openapi_schema",
                "proto_service",
                "proto_rpc",
                "proto_message",
            ],
            query_terms=query_terms,
            explanation="query asks where behavior, modules, or symbols are implemented",
        )
    if _contains_any(lower, _ROOT_CAUSE_TERMS):
        return QueryIntent(
            intent=INTENT_RUNTIME_INCIDENT,
            secondary_intents=[
                intent
                for intent, terms in (
                    (INTENT_RUNTIME_INCIDENT, _RUNTIME_TERMS),
                    (INTENT_DEPLOY_REGRESSION, _DEPLOY_TERMS),
                    (INTENT_PREVIOUS_INCIDENT, _INCIDENT_HISTORY_TERMS),
                )
                if _contains_any(lower, terms)
            ],
            preferred_source_types=["logs", "deploy", "incident", "runbook", "code"],
            preferred_chunk_types=["log_time_window", "log_error_burst", "deploy_commit", "deploy_diff", "incident_root_cause", "incident_section", "go_function", "markdown_heading_section"],
            query_terms=query_terms,
            explanation="query asks for incident explanation, causality, or investigation support",
        )
    if _contains_any(lower, _DEPLOY_TERMS) and not _contains_any(lower, {"where is", "where are"}):
        return QueryIntent(
            intent=INTENT_DEPLOY_REGRESSION,
            preferred_source_types=["deploy", "logs", "incident", "code"],
            preferred_chunk_types=["deploy_commit", "deploy_diff", "release_note", "log_time_window", "go_function", "config_service_block", "config_section"],
            query_terms=query_terms,
            explanation="query asks about deploys, releases, diffs, or changes before an incident",
        )
    if _contains_any(lower, _RUNTIME_TERMS):
        return QueryIntent(
            intent=INTENT_RUNTIME_INCIDENT,
            preferred_source_types=["logs", "runbook", "deploy", "incident"],
            preferred_chunk_types=["log_time_window", "log_error_burst", "markdown_procedure", "deploy_commit", "deploy_diff"],
            query_terms=query_terms,
            explanation="query asks for runtime symptoms, errors, traces, or timeouts",
        )
    if _contains_any(lower, _API_TERMS):
        return QueryIntent(
            intent=INTENT_API_CONTRACT,
            secondary_intents=[INTENT_CODE_LOCATION] if "implemented" in lower else [],
            preferred_source_types=["api_doc", "code", "config"],
            preferred_chunk_types=["openapi_endpoint", "openapi_schema", "proto_service", "proto_rpc", "proto_message", "proto_enum", "go_function", "markdown_heading_section"],
            query_terms=query_terms,
            explanation="query asks about API, route, RPC, protobuf, or endpoint contracts",
        )
    if _contains_any(lower, _CONFIG_TERMS):
        return QueryIntent(
            intent=INTENT_CONFIG_LOOKUP,
            secondary_intents=[INTENT_CODE_LOCATION] if "implemented" in lower else [],
            preferred_source_types=["config", "deploy", "code"],
            preferred_chunk_types=["config_service_block", "env_var_block", "dependency_block", "config_section", "deploy_commit", "deploy_diff", "markdown_heading_section", "go_function"],
            query_terms=query_terms,
            explanation="query asks about configuration, deployment config, environment, or dependency settings",
        )
    if _contains_any(lower, _ARCHITECTURE_TERMS):
        return QueryIntent(
            intent=INTENT_ARCHITECTURE,
            preferred_source_types=["runbook", "api_doc", "code", "config"],
            preferred_chunk_types=["markdown_heading_section", "markdown_table", "python_module", "go_module", "proto_service", "proto_message", "config_service_block", "config_section"],
            query_terms=query_terms,
            explanation="query asks for architecture, boundaries, or design context",
        )
    return QueryIntent(
        intent=INTENT_GENERIC,
        preferred_source_types=["runbook", "code", "config", "api_doc"],
        preferred_chunk_types=["markdown_heading_section", "python_module", "go_module", "go_function", "config_section"],
        query_terms=query_terms,
        explanation="balanced retrieval because query intent is ambiguous",
    )


def investigate_supported(intent: QueryIntent, available_source_types: set[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if intent.intent in {INTENT_CODE_LOCATION, INTENT_CONFIG_LOOKUP, INTENT_ARCHITECTURE, INTENT_API_CONTRACT, INTENT_GENERIC}:
        reasons.append("query intent is repo lookup rather than runtime incident investigation")
        return False, reasons
    if intent.intent == INTENT_RUNTIME_INCIDENT and "logs" not in available_source_types:
        reasons.append("runtime investigation requires logs")
        return False, reasons
    if intent.intent == INTENT_DEPLOY_REGRESSION and "deploy" not in available_source_types:
        reasons.append("deploy analysis requires deploy history or diffs")
        return False, reasons
    if intent.intent == INTENT_PREVIOUS_INCIDENT and "incident" not in available_source_types:
        reasons.append("incident history lookup requires incident reports or postmortems")
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
