"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type Json = Record<string, unknown>;
type Project = { project_id: string; name: string; role: string; created_at: string };
type Source = { id: string; name: string; source_type: string; status: string; last_sync_status?: string | null; last_sync_finished_at?: string | null };
type SearchHit = { chunk_id: string; rank: number; document_path: string; source_type: string; score: number; text_preview: string; citation?: { lines?: string } };
type SearchResult = { total: number; latency_ms: number; query_intent?: string; results: SearchHit[]; evidence_mix?: Record<string, Record<string, number>>; debug?: Json | null };
type Readiness = { score: number; grade: string; summary: string; counts: Json; coverage: Json; missing_evidence: string[]; suggested_actions: string[]; suggested_questions: string[]; warnings: string[]; latest_sync: Json };
type Operation = { operational_run_id: string; run_type: string; status: string; attempts: number; created_at: string; summary: Json };
type Finding = { finding_id: string; finding_type: string; severity: string; recommended_action: string; created_at: string };
type EvalRun = { eval_run_id: string; project_id: string; status: string; created_at: string; summary: Json };
type RunEvent = { sequence_no: number; event_type: string; node_name?: string | null; created_at: string };

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "/api";

function errorDetail(payload: unknown): string {
  if (typeof payload === "object" && payload && "detail" in payload) {
    const detail = (payload as Json).detail;
    return typeof detail === "string" ? detail : "Request failed";
  }
  return "Request failed";
}

function badgeClass(value?: string | null): string {
  const normalized = (value || "unknown").toLowerCase();
  if (["success", "completed", "good", "excellent", "ready", "active", "high"].includes(normalized)) return "good";
  if (["failed", "empty", "weak", "error", "low"].includes(normalized)) return "bad";
  if (["partial", "queued", "running", "pending", "waiting_for_approval", "medium"].includes(normalized)) return "warn";
  return "";
}

function valueOrUnknown(value: unknown): string {
  return value === null || value === undefined || value === "" ? "unknown" : String(value);
}

export default function HomePage() {
  const [email, setEmail] = useState("admin@incidentops.local");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [projectName, setProjectName] = useState("engineering-evidence");
  const [runtime, setRuntime] = useState<Json | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [evals, setEvals] = useState<EvalRun[]>([]);
  const [query, setQuery] = useState("Where is the relevant service implementation defined?");
  const [search, setSearch] = useState<SearchResult | null>(null);
  const [investigation, setInvestigation] = useState<Json | null>(null);
  const [workflow, setWorkflow] = useState<Json | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);
  const [notice, setNotice] = useState("Sign in to load projects and live runtime status.");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const authHeaders = useMemo(() => (token ? { Authorization: `Bearer ${token}` } : {}), [token]);

  useEffect(() => {
    setToken(window.sessionStorage.getItem("incidentops.access_token") || "");
    setProjectId(window.localStorage.getItem("incidentops.project_id") || "");
  }, []);

  const request = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const headers = new Headers(init.headers);
    for (const [name, value] of Object.entries(authHeaders)) headers.set(name, value);
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
    const payload: unknown = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(payload));
    return payload as T;
  }, [authHeaders]);

  const loadProjects = useCallback(async () => {
    if (!token) return;
    const payload = await request<Project[]>("/v1/projects");
    setProjects(payload);
    if (!projectId && payload[0]) setProjectId(payload[0].project_id);
  }, [projectId, request, token]);

  const loadProjectData = useCallback(async (id = projectId) => {
    if (!token || !id) return;
    setBusy("refresh"); setError("");
    try {
      const [runtimePayload, readinessPayload, sourcePayload, operationPayload, findingPayload, evalPayload] = await Promise.all([
        request<Json>("/v1/runtime/status"),
        request<Readiness>(`/v1/projects/${id}/readiness`),
        request<Source[]>(`/v1/projects/${id}/sources`),
        request<Operation[]>(`/v1/projects/${id}/operations/runs`),
        request<Finding[]>(`/v1/projects/${id}/operations/findings`),
        request<EvalRun[]>("/v1/evals"),
      ]);
      setRuntime(runtimePayload); setReadiness(readinessPayload); setSources(sourcePayload);
      setOperations(operationPayload); setFindings(findingPayload);
      setEvals(evalPayload.filter((evaluation) => evaluation.project_id === id));
      window.localStorage.setItem("incidentops.project_id", id);
      setNotice("Live project state refreshed from Core.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to load project state");
    } finally { setBusy(null); }
  }, [projectId, request, token]);

  useEffect(() => { void loadProjects().catch((caught) => setError(caught instanceof Error ? caught.message : "Unable to list projects")); }, [loadProjects]);
  useEffect(() => { void loadProjectData(); }, [loadProjectData]);

  async function signIn(event: FormEvent) {
    event.preventDefault(); setBusy("login"); setError("");
    try {
      const payload = await request<{ access_token: string; user: { email: string } }>("/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      window.sessionStorage.setItem("incidentops.access_token", payload.access_token);
      setToken(payload.access_token); setPassword(""); setNotice(`Signed in as ${payload.user.email}.`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Sign-in failed"); }
    finally { setBusy(null); }
  }

  async function createProject() {
    if (!projectName.trim()) return;
    setBusy("project"); setError("");
    try {
      const payload = await request<{ project_id: string; name: string }>("/v1/projects", { method: "POST", body: JSON.stringify({ name: projectName.trim(), demo_mode: false }) });
      setProjectId(payload.project_id); setNotice(`Created ${payload.name}. Use a Collector to register and sync a source.`);
      await loadProjects();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Project creation failed"); }
    finally { setBusy(null); }
  }

  async function submitSearch() {
    if (!projectId || !query.trim()) return;
    setBusy("search"); setError("");
    try {
      const payload = await request<SearchResult>("/v1/search", { method: "POST", body: JSON.stringify({ project_id: projectId, query, top_k: 8, debug: true }) });
      setSearch(payload); setNotice(`Search returned ${payload.total} cited evidence item(s).`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Search failed"); }
    finally { setBusy(null); }
  }

  async function submitInvestigation() {
    if (!projectId || !query.trim()) return;
    setBusy("investigate"); setError("");
    try {
      const payload = await request<Json>("/v1/investigate", { method: "POST", body: JSON.stringify({ project_id: projectId, query, top_k: 8, debug: true }) });
      setInvestigation(payload); setNotice("Investigation completed. Review confidence and missing evidence before acting.");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Investigation failed"); }
    finally { setBusy(null); }
  }

  async function createWorkflow() {
    if (!projectId || !query.trim()) return;
    setBusy("workflow"); setError("");
    try {
      const payload = await request<Json>("/v1/runs", { method: "POST", body: JSON.stringify({ project_id: projectId, query, top_k: 8, create_issue_draft: true }) });
      setWorkflow(payload); setRunEvents([]); setNotice(`Workflow ${valueOrUnknown(payload.run_id)} created.`);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Workflow creation failed"); }
    finally { setBusy(null); }
  }

  async function loadWorkflowEvents() {
    const runId = workflow?.run_id;
    if (typeof runId !== "string") return;
    setBusy("events");
    try { setRunEvents(await request<RunEvent[]>(`/v1/runs/${runId}/events`)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to load workflow events"); }
    finally { setBusy(null); }
  }

  async function trigger(kind: "observer" | "logging" | "eval") {
    if (!projectId) return;
    setBusy(kind); setError("");
    try {
      if (kind === "eval") await request<EvalRun>("/v1/evals/run", { method: "POST", body: JSON.stringify({ project_id: projectId, top_k: 8 }) });
      else await request<Operation>(`/v1/projects/${projectId}/operations/${kind}/runs`, { method: "POST", body: JSON.stringify({ idempotency_key: `${kind}-${Date.now()}` }) });
      await loadProjectData();
    } catch (caught) { setError(caught instanceof Error ? caught.message : `Unable to start ${kind}`); }
    finally { setBusy(null); }
  }

  async function reindexSource(source: Source) {
    if (!projectId || !window.confirm(`Refresh Core-held chunks and lexical state for ${source.name}? This does not re-read the external repository.`)) return;
    setBusy(`reindex-${source.id}`); setError("");
    try { await request<Json>(`/v1/projects/${projectId}/sources/${source.id}/reindex`, { method: "POST" }); await loadProjectData(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Refresh failed"); }
    finally { setBusy(null); }
  }

  async function purgeSource(source: Source) {
    if (!projectId || !window.confirm(`Delete ${source.name}, all indexed chunks, and sync state? This cannot be undone.`)) return;
    setBusy(`purge-${source.id}`); setError("");
    try { await request<Json>(`/v1/projects/${projectId}/sources/${source.id}`, { method: "DELETE" }); await loadProjectData(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Source deletion failed"); }
    finally { setBusy(null); }
  }

  function signOut() {
    window.sessionStorage.removeItem("incidentops.access_token");
    setToken(""); setProjects([]); setRuntime(null); setReadiness(null); setSources([]); setNotice("Signed out.");
  }

  const citations = Array.isArray(investigation?.citations) ? investigation.citations as Json[] : [];
  const missingData = Array.isArray(investigation?.missing_data) ? investigation.missing_data as string[] : [];

  return <main className="shell">
    <header className="topbar"><div><div className="brand">IncidentOps Console</div><div className="subbrand">Engineering evidence, retrieval diagnostics, and bounded investigation.</div></div><div className="identity">{token ? <><div>Authenticated browser session</div><button className="secondary" onClick={signOut}>Sign out</button></> : "Authentication required"}</div></header>
    <div className="layout">
      {error && <div className="notice error">{error}</div>}
      <div className="notice">{notice}</div>
      {!token && <section className="panel"><div className="panel-head"><h2>Sign in</h2><span className="muted">The JWT remains in this browser session only.</span></div><form className="grid grid-3" onSubmit={signIn}><label>Email<input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label><label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label><div className="row"><button disabled={busy === "login"}>{busy === "login" ? "Signing in..." : "Sign in"}</button></div></form></section>}
      {token && <>
        <section className="panel"><div className="panel-head"><h2>Project context</h2><button className="secondary" onClick={() => void loadProjectData()} disabled={busy === "refresh"}>{busy === "refresh" ? "Refreshing..." : "Refresh Core state"}</button></div><div className="grid grid-3"><label>Accessible project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">Select a project</option>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name} ({project.role})</option>)}</select></label><label>New project name<input value={projectName} onChange={(event) => setProjectName(event.target.value)} /></label><div className="row"><button onClick={() => void createProject()} disabled={busy === "project"}>{busy === "project" ? "Creating..." : "Create project"}</button></div></div><p className="muted">Evidence is ingested by an authenticated Collector. The browser never scans a server path or calls Qdrant, PostgreSQL, Redis, or model endpoints.</p></section>

        <section className="panel"><div className="panel-head"><h2>Runtime status</h2>{runtime && <span className={`badge ${runtime.local_fallback_active ? "bad" : "good"}`}>{runtime.local_fallback_active ? "fallback active" : "cloud path configured"}</span>}</div>{runtime ? <div className="grid grid-4">{[["Environment", runtime.app_env], ["Vector store", runtime.retrieval_backend], ["Embedding", runtime.embedding_backend], ["Worker", runtime.worker_mode], ["LLM", runtime.llm_provider], ["Reranking", runtime.rag_rerank_mode], ["MCP", runtime.mcp_enabled ? "enabled" : "disabled"], ["Index version", runtime.vector_index_version]].map(([label, value]) => <div className="stat" key={String(label)}><strong>{valueOrUnknown(value)}</strong><span>{String(label)}</span></div>)}</div> : <p className="muted">Runtime status loads after choosing a project.</p>}</section>

        <section className="grid grid-3">
          <div className="panel"><div className="panel-head"><h2>Readiness</h2>{readiness && <span className={`badge ${badgeClass(readiness.grade)}`}>{readiness.grade}</span>}</div>{readiness ? <div className="stack"><div className="stat"><strong>{readiness.score}/100</strong><span>{readiness.summary}</span></div><div className="metric-list">{Object.entries(readiness.counts).map(([key, value]) => <div key={key}><strong>{valueOrUnknown(value)}</strong>{key.replaceAll("_", " ")}</div>)}</div><h3>Missing evidence</h3><ul className="list">{readiness.missing_evidence.length ? readiness.missing_evidence.map((item) => <li key={item}>{item}</li>) : <li>None reported</li>}</ul></div> : <p className="muted">Select a project to load readiness.</p>}</div>
          <div className="panel"><div className="panel-head"><h2>Suggested next actions</h2></div>{readiness ? <><ul className="list">{readiness.suggested_actions.map((item) => <li key={item}>{item}</li>)}</ul><h3 style={{ marginTop:16 }}>Suggested questions</h3><div className="stack">{readiness.suggested_questions.map((item) => <button className="secondary" key={item} onClick={() => setQuery(item)}>{item}</button>)}</div></> : <p className="muted">No readiness report loaded.</p>}</div>
          <div className="panel"><div className="panel-head"><h2>Collector sources</h2><span className="muted">{sources.length} source(s)</span></div><div className="stack">{sources.map((source) => <div key={source.id} className="evidence"><strong>{source.name}</strong> <span className={`badge ${badgeClass(source.last_sync_status || source.status)}`}>{source.last_sync_status || source.status}</span><div className="muted">{source.source_type} · last completed {source.last_sync_finished_at || "never"}</div><div className="row" style={{ marginTop:8 }}><button className="secondary" onClick={() => void reindexSource(source)} disabled={busy === `reindex-${source.id}`}>Refresh index</button><button className="danger" onClick={() => void purgeSource(source)} disabled={busy === `purge-${source.id}`}>Purge source</button></div></div>)}{!sources.length && <p className="muted">No source has been registered. Start a Collector sync for this project.</p>}</div></div>
        </section>

        <section className="panel"><div className="panel-head"><h2>Evidence retrieval</h2><span className="muted">Core returns project-scoped cited evidence.</span></div><div className="stack"><textarea value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Evidence query" /><div className="row"><button onClick={() => void submitSearch()} disabled={!projectId || busy === "search"}>{busy === "search" ? "Searching..." : "Search evidence"}</button><button className="secondary" onClick={() => void submitInvestigation()} disabled={!projectId || busy === "investigate"}>{busy === "investigate" ? "Investigating..." : "Investigate"}</button><button className="secondary" onClick={() => void createWorkflow()} disabled={!projectId || busy === "workflow"}>{busy === "workflow" ? "Creating..." : "Create workflow"}</button></div></div>{search && <div className="grid grid-3" style={{ marginTop:16 }}><div className="stat"><strong>{search.total}</strong><span>evidence results</span></div><div className="stat"><strong>{search.latency_ms} ms</strong><span>Core search latency</span></div><div className="stat"><strong>{search.query_intent || "generic"}</strong><span>query intent</span></div></div>}{search?.results.map((hit) => <article className="evidence" key={hit.chunk_id}><div className="row"><strong>#{hit.rank} {hit.document_path}</strong><span className="badge">{hit.source_type}</span><span className="badge">score {hit.score}</span></div><div className="path">{hit.citation?.lines || "line range unavailable"}</div><p className="preview">{hit.text_preview}</p></article>)}</section>

        <section className="grid grid-2"><div className="panel"><div className="panel-head"><h2>Investigation</h2>{investigation && <span className={`badge ${badgeClass(String(investigation.confidence || "unknown"))}`}>{String(investigation.confidence || "unknown")}</span>}</div>{investigation ? <div className="stack"><strong>{valueOrUnknown((investigation.likely_root_cause as Json | undefined)?.summary)}</strong><div className="muted">{valueOrUnknown(investigation.query_intent)} · {valueOrUnknown(investigation.latency_ms)} ms · {citations.length} citation(s)</div>{missingData.length > 0 && <div className="notice warn"><strong>Missing evidence</strong><ul className="list">{missingData.map((item) => <li key={item}>{item}</li>)}</ul></div>}<h3>Citations</h3><ul className="list">{citations.map((citation, index) => <li key={index}>{valueOrUnknown(citation.path || citation.label)} {citation.lines ? `(${String(citation.lines)})` : ""}</li>)}</ul></div> : <p className="muted">Run an investigation to see confidence, citations, and explicit missing-data warnings.</p>}</div>
        <div className="panel"><div className="panel-head"><h2>Workflow</h2><button className="secondary" onClick={() => void loadWorkflowEvents()} disabled={!workflow || busy === "events"}>{busy === "events" ? "Loading..." : "Load events"}</button></div>{workflow ? <div className="stack"><div className="row"><span className={`badge ${badgeClass(String(workflow.status || ""))}`}>{valueOrUnknown(workflow.status)}</span><span className="muted">{valueOrUnknown(workflow.run_id)}</span></div><ul className="list">{runEvents.map((event) => <li key={`${event.sequence_no}-${event.event_type}`}>{event.sequence_no}. {event.event_type}{event.node_name ? ` (${event.node_name})` : ""}</li>)}</ul></div> : <p className="muted">Workflow runs are queued through Core workers and retain approval state.</p>}</div></section>

        <section className="grid grid-3"><div className="panel"><div className="panel-head"><h2>Evaluations</h2><button className="secondary" onClick={() => void trigger("eval")} disabled={!projectId || busy === "eval"}>{busy === "eval" ? "Starting..." : "Run eval"}</button></div><div className="stack">{evals.map((evaluation) => <div className="evidence" key={evaluation.eval_run_id}><span className={`badge ${badgeClass(evaluation.status)}`}>{evaluation.status}</span><div className="muted">{evaluation.created_at}</div><pre>{JSON.stringify(evaluation.summary, null, 2)}</pre></div>)}{!evals.length && <p className="muted">No evaluation runs recorded for this project.</p>}</div></div>
        <div className="panel"><div className="panel-head"><h2>RAG operations</h2><div className="row"><button className="secondary" onClick={() => void trigger("observer")} disabled={!projectId || busy === "observer"}>Run observer</button><button className="secondary" onClick={() => void trigger("logging")} disabled={!projectId || busy === "logging"}>Aggregate events</button></div></div><div className="stack">{operations.map((operation) => <div className="evidence" key={operation.operational_run_id}><span className={`badge ${badgeClass(operation.status)}`}>{operation.status}</span> <strong>{operation.run_type}</strong><div className="muted">attempts {operation.attempts} · {operation.created_at}</div></div>)}{!operations.length && <p className="muted">No operational runs recorded.</p>}</div></div>
        <div className="panel"><div className="panel-head"><h2>Observer findings</h2><span className="muted">{findings.length} finding(s)</span></div><div className="stack">{findings.map((finding) => <div className="evidence" key={finding.finding_id}><span className={`badge ${badgeClass(finding.severity)}`}>{finding.severity}</span> <strong>{finding.finding_type}</strong><div className="muted">{finding.recommended_action}</div></div>)}{!findings.length && <p className="muted">No findings recorded. This means none are loaded, not that the system is healthy.</p>}</div></div></section>
      </>}
    </div>
  </main>;
}
