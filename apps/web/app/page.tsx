"use client";

import {
  Activity,
  CheckCircle2,
  ChevronRight,
  Clipboard,
  Database,
  FileSearch,
  FolderGit2,
  GitBranch,
  LogIn,
  LogOut,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
  TriangleAlert,
  Workflow,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

type ApiError = { detail?: string; message?: string };
type Project = { project_id: string; name: string; role: string; created_at?: string };
type Source = {
  id: string;
  name: string;
  source_type: string;
  status: string;
  sync_mode: string;
  last_sync_status?: string | null;
  last_sync_finished_at?: string | null;
};
type Sync = {
  sync_id: string;
  status: string;
  files_seen: number;
  documents_received: number;
  chunks_created: number;
  files_skipped: number;
  parser_errors: number;
  finished_at?: string | null;
  diagnostics?: Record<string, unknown>;
};
type Runtime = {
  app_env?: string;
  llm_provider?: string;
  embedding_backend?: string;
  embedding_dimension?: number;
  retrieval_backend?: string;
  vector_collection?: string;
  worker_mode?: string;
  mcp_enabled?: boolean;
  local_fallback_active?: boolean;
};
type Readiness = {
  score: number;
  grade: string;
  summary: string;
  coverage: Record<string, boolean>;
  counts: { documents: number; chunks: number; sources: number; failed_syncs: number };
  latest_sync?: { status?: string | null; duration_seconds?: number | null; parser_errors?: number };
  missing_evidence: string[];
  answerable_questions: string[];
  weak_questions: string[];
  suggested_questions: string[];
  suggested_actions: string[];
  warnings: string[];
};
type Citation = { label?: string; path?: string; lines?: string; location?: string };
type Evidence = {
  chunk_id: string;
  source_type: string;
  chunk_type?: string;
  document_path: string;
  score: number;
  text_preview: string;
  citation: Citation;
  why_retrieved?: string[];
};
type SearchResponse = {
  total: number;
  latency_ms: number;
  query_intent?: string;
  evidence_mix?: { source_types?: Record<string, number> };
  results: Evidence[];
  debug?: Record<string, unknown>;
};
type Investigation = {
  answer?: string | null;
  task_type: string;
  query_intent: string;
  investigation_supported: boolean;
  confidence: string;
  confidence_reasons: string[];
  likely_root_cause: { summary: string; confidence: string };
  suggested_fix?: string | null;
  citations: Citation[];
  missing_data: string[];
  unknowns: string[];
  latency_ms: number;
};
type WorkflowRun = {
  run_id: string;
  status: string;
  task_type?: string | null;
  risk_level?: string | null;
  pending_approval: boolean;
  error?: string | null;
  updated_at?: string | null;
};
type RunEvent = { sequence_no: number; event_type: string; node_name?: string | null; created_at: string };

const DEFAULT_API = process.env.NEXT_PUBLIC_CORE_API_URL ?? "";
const DEFAULT_SEARCH = "Where is the service configured?";
const DEFAULT_INVESTIGATION = "What evidence exists for investigating a latency regression?";

function normalizeApiUrl(value: string) {
  return value.trim().replace(/\/$/, "");
}

function asError(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object") {
    const candidate = payload as ApiError;
    if (typeof candidate.detail === "string") return candidate.detail;
    if (typeof candidate.message === "string") return candidate.message;
  }
  return fallback;
}

function dateLabel(value?: string | null) {
  if (!value) return "Not yet";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function statusTone(value?: string | null) {
  const normalized = (value ?? "").toLowerCase();
  if (["success", "completed", "ready", "active"].includes(normalized)) return "good";
  if (["failed", "error", "cancelled"].includes(normalized)) return "bad";
  if (["running", "queued", "waiting_for_approval", "partial_success", "syncing"].includes(normalized)) return "warn";
  return "quiet";
}

export default function IncidentOpsWorkspace() {
  const [apiUrl, setApiUrl] = useState(DEFAULT_API);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [health, setHealth] = useState<"unknown" | "healthy" | "unready" | "offline">("unknown");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [syncs, setSyncs] = useState<Record<string, Sync>>({});
  const [searchQuery, setSearchQuery] = useState(DEFAULT_SEARCH);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const [investigationQuery, setInvestigationQuery] = useState(DEFAULT_INVESTIGATION);
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [newProjectName, setNewProjectName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("Connect Core, then sign in to begin an evidence investigation.");
  const [activeView, setActiveView] = useState<"overview" | "sources" | "search" | "investigate" | "runs">("overview");

  const apiOrigin = useMemo(() => normalizeApiUrl(apiUrl), [apiUrl]);
  const selectedProject = projects.find((project) => project.project_id === projectId);
  const authenticated = Boolean(token);

  useEffect(() => {
    const savedApi = window.sessionStorage.getItem("incidentops.api-url");
    const savedToken = window.sessionStorage.getItem("incidentops.access-token");
    if (savedApi && !DEFAULT_API) setApiUrl(savedApi);
    if (savedToken) setToken(savedToken);
  }, []);

  useEffect(() => {
    if (authenticated && projectId) void refreshProject();
  }, [projectId, authenticated]);

  async function request<T>(path: string, init: RequestInit = {}, needsAuth = true): Promise<T> {
    if (!apiOrigin) throw new Error("Enter the Core API URL first.");
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    if (needsAuth && token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${apiOrigin}${path}`, { ...init, headers });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(asError(payload, `Core returned HTTP ${response.status}`));
    return payload as T;
  }

  async function checkCore() {
    setBusy("health");
    try {
      const [healthResponse, readyResponse] = await Promise.all([
        request<{ status?: string }>("/health", {}, false),
        request<{ status?: string }>("/ready", {}, false),
      ]);
      const ready = healthResponse.status === "ok" && readyResponse.status === "ready";
      setHealth(ready ? "healthy" : "unready");
      setNotice(ready ? "Core is ready. Sign in to inspect evidence." : "Core is reachable, but readiness checks are not yet satisfied.");
      window.sessionStorage.setItem("incidentops.api-url", apiOrigin);
    } catch (error) {
      setHealth("offline");
      setNotice(error instanceof Error ? error.message : "Core could not be reached.");
    } finally {
      setBusy(null);
    }
  }

  async function loadProjects(activeToken = token) {
    if (!activeToken) return;
    const response = await fetch(`${apiOrigin}/v1/projects`, { headers: { Authorization: `Bearer ${activeToken}`, Accept: "application/json" } });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(asError(payload, "Could not load projects."));
    const nextProjects = payload as Project[];
    setProjects(nextProjects);
    setProjectId((current) => current || nextProjects[0]?.project_id || "");
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("login");
    try {
      const result = await request<{ access_token: string; user: { email: string } }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }, false);
      setToken(result.access_token);
      window.sessionStorage.setItem("incidentops.access-token", result.access_token);
      window.sessionStorage.setItem("incidentops.api-url", apiOrigin);
      await Promise.all([
        loadProjects(result.access_token),
        fetch(`${apiOrigin}/v1/runtime/status`, { headers: { Authorization: `Bearer ${result.access_token}` } })
          .then((response) => response.ok ? response.json() as Promise<Runtime> : null)
          .then((data) => data && setRuntime(data)),
      ]);
      setNotice(`Signed in as ${result.user.email}. Select a project or create one.`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Sign-in failed.");
    } finally {
      setBusy(null);
    }
  }

  function signOut() {
    window.sessionStorage.removeItem("incidentops.access-token");
    setToken("");
    setProjects([]);
    setProjectId("");
    setReadiness(null);
    setSources([]);
    setSyncs({});
    setSearchResult(null);
    setInvestigation(null);
    setWorkflow(null);
    setEvents([]);
    setRuntime(null);
    setNotice("Signed out. The token was removed from this browser session.");
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newProjectName.trim()) return;
    setBusy("project");
    try {
      const project = await request<{ project_id: string; name: string }>("/v1/projects", {
        method: "POST",
        body: JSON.stringify({ name: newProjectName.trim(), demo_mode: false }),
      });
      await loadProjects();
      setProjectId(project.project_id);
      setNewProjectName("");
      setNotice(`Created project ${project.name}. Add a source next.`);
      setActiveView("sources");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not create project.");
    } finally {
      setBusy(null);
    }
  }

  async function refreshProject() {
    if (!projectId) return;
    setBusy("refresh-project");
    try {
      const [readinessResult, sourceResult] = await Promise.all([
        request<Readiness>(`/v1/projects/${projectId}/readiness`),
        request<Source[]>(`/v1/projects/${projectId}/sources`),
      ]);
      setReadiness(readinessResult);
      setSources(sourceResult);
      const latest = await Promise.all(sourceResult.map(async (source) => {
        try {
          return [source.id, await request<Sync>(`/v1/sources/${source.id}/syncs/latest`)] as const;
        } catch { return [source.id, null] as const; }
      }));
      setSyncs(Object.fromEntries(latest.filter((entry): entry is readonly [string, Sync] => entry[1] !== null)));
      setNotice("Project evidence state refreshed.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not refresh this project.");
    } finally {
      setBusy(null);
    }
  }

  async function addSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return setNotice("Create or select a project first.");
    const trimmedUrl = repoUrl.trim();
    if (!/^https:\/\/(github\.com|www\.github\.com)\/.+/.test(trimmedUrl)) {
      return setNotice("Use a public GitHub repository URL. Credentials do not belong in source configuration.");
    }
    setBusy("source");
    try {
      const inferredName = sourceName.trim() || trimmedUrl.replace(/\/$/, "").split("/").pop() || "github-source";
      await request<Source>(`/v1/projects/${projectId}/sources`, {
        method: "POST",
        body: JSON.stringify({
          name: inferredName,
          source_type: "git",
          sync_mode: "collector",
          config: { repository_url: trimmedUrl },
        }),
      });
      setRepoUrl("");
      setSourceName("");
      await refreshProject();
      setNotice("Source registered. The deployed Collector must be configured for this repository before a sync can begin.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not register source.");
    } finally {
      setBusy(null);
    }
  }

  async function runSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return setNotice("Select a project before searching.");
    setBusy("search");
    try {
      const result = await request<SearchResponse>("/v1/search", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, query: searchQuery, top_k: 8, debug: false }),
      });
      setSearchResult(result);
      setActiveView("search");
      setNotice(result.total ? `${result.total} evidence item${result.total === 1 ? "" : "s"} retrieved.` : "No evidence matched. Inspect source coverage before broadening the claim.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Search failed.");
    } finally {
      setBusy(null);
    }
  }

  async function runInvestigation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return setNotice("Select a project before investigating.");
    setBusy("investigate");
    try {
      const result = await request<Investigation>("/v1/investigate", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, query: investigationQuery, top_k: 8, debug: false }),
      });
      setInvestigation(result);
      setActiveView("investigate");
      setNotice(result.investigation_supported ? "Investigation completed with cited evidence." : "Investigation correctly identified insufficient evidence.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Investigation failed.");
    } finally {
      setBusy(null);
    }
  }

  async function startWorkflow() {
    if (!projectId) return setNotice("Select a project before starting a workflow run.");
    setBusy("workflow");
    try {
      const result = await request<WorkflowRun>("/v1/runs", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, query: investigationQuery, top_k: 8, create_issue_draft: true }),
      });
      setWorkflow(result);
      setActiveView("runs");
      await pollRun(result.run_id);
      setNotice(`Workflow ${result.status}. Review its events and approval state.`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not start workflow.");
    } finally {
      setBusy(null);
    }
  }

  async function pollRun(runId: string) {
    for (let count = 0; count < 20; count += 1) {
      const [run, runEvents] = await Promise.all([
        request<WorkflowRun>(`/v1/runs/${runId}`),
        request<RunEvent[]>(`/v1/runs/${runId}/events`),
      ]);
      setWorkflow(run);
      setEvents(runEvents);
      if (["completed", "failed", "cancelled", "waiting_for_approval"].includes(run.status)) return;
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }

  function useSuggestedQuestion(question: string, target: "search" | "investigate") {
    if (target === "search") {
      setSearchQuery(question);
      setActiveView("search");
    } else {
      setInvestigationQuery(question);
      setActiveView("investigate");
    }
  }

  const coverageEntries = readiness ? Object.entries(readiness.coverage).filter(([, value]) => value) : [];
  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#workspace" aria-label="IncidentOps workspace">
          <span className="brand-mark"><span>IO</span></span>
          <span>IncidentOps</span>
        </a>
        <div className="topbar-actions">
          <span className={`service-dot ${health}`} aria-hidden="true" />
          <span className="service-label">{health === "healthy" ? "Core ready" : health === "offline" ? "Core offline" : "Evidence workspace"}</span>
          {authenticated ? <button className="text-button" onClick={signOut}><LogOut size={15} /> Sign out</button> : null}
        </div>
      </header>

      <section className="hero" id="workspace">
        <div className="hero-grid" aria-hidden="true" />
        <div className="hero-copy">
          <p className="kicker">Engineering evidence, under pressure</p>
          <h1>Find the facts before the story hardens.</h1>
          <p>Connect Core, register the evidence you own, and trace every search or investigation back to a source, a sync, and a citation.</p>
        </div>
        <div className="chain-card" aria-label="Active evidence chain">
          <p className="kicker">Evidence chain</p>
          <ol>
            <li className={apiOrigin ? "complete" : ""}><span>01</span> Core connection</li>
            <li className={authenticated ? "complete" : ""}><span>02</span> Authenticated operator</li>
            <li className={projectId ? "complete" : ""}><span>03</span> Project context</li>
            <li className={sources.length ? "complete" : ""}><span>04</span> Source and sync</li>
            <li className={searchResult || investigation ? "complete" : ""}><span>05</span> Cited decision</li>
          </ol>
        </div>
      </section>

      <div className="notice" role="status"><Activity size={15} /> {notice}</div>

      <section className="operator-layout">
        <aside className="command-rail">
          <section>
            <div className="rail-title"><span>Connection</span><button className="icon-button" onClick={checkCore} disabled={!apiOrigin || busy === "health"} title="Check Core health"><RefreshCw size={15} className={busy === "health" ? "spin" : ""} /></button></div>
            <label htmlFor="api-url">Core API</label>
            <input id="api-url" value={apiUrl} onChange={(event) => setApiUrl(event.target.value)} placeholder="https://core.example.com" inputMode="url" />
            <button className="secondary-button" onClick={checkCore} disabled={!apiOrigin || busy === "health"}><Activity size={15} /> Check service</button>
          </section>

          <section>
            <div className="rail-title"><span>Operator</span><span className={`tag ${authenticated ? "good" : "quiet"}`}>{authenticated ? "signed in" : "required"}</span></div>
            {authenticated ? <p className="rail-copy">Your session is held only in this browser tab.</p> : (
              <form className="compact-form" onSubmit={signIn}>
                <label htmlFor="email">Email</label>
                <input id="email" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
                <label htmlFor="password">Password</label>
                <input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
                <button className="primary-button" type="submit" disabled={!apiOrigin || busy === "login"}><LogIn size={15} /> Sign in</button>
              </form>
            )}
          </section>

          <section>
            <div className="rail-title"><span>Project context</span><button className="icon-button" onClick={() => loadProjects().catch((error) => setNotice(error.message))} disabled={!authenticated} title="Refresh projects"><RefreshCw size={15} /></button></div>
            <select aria-label="Select project" value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={!authenticated}>
              <option value="">Select a project</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
            </select>
            <form className="inline-create" onSubmit={createProject}>
              <input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} placeholder="New project name" disabled={!authenticated} aria-label="New project name" />
              <button className="icon-button accent" type="submit" disabled={!authenticated || busy === "project"} title="Create project"><Plus size={16} /></button>
            </form>
          </section>

          <section className="runtime-card">
            <div className="rail-title"><span>Runtime</span><span className={`tag ${runtime?.local_fallback_active ? "bad" : runtime ? "good" : "quiet"}`}>{runtime ? "live" : "unknown"}</span></div>
            {runtime ? <dl>
              <div><dt>Embed</dt><dd>{runtime.embedding_backend ?? "unknown"}</dd></div>
              <div><dt>Vector</dt><dd>{runtime.retrieval_backend ?? "unknown"}</dd></div>
              <div><dt>Worker</dt><dd>{runtime.worker_mode ?? "unknown"}</dd></div>
              <div><dt>Dimension</dt><dd>{runtime.embedding_dimension ?? "?"}</dd></div>
            </dl> : <p className="rail-copy">Sign in to inspect deployed runtime configuration.</p>}
          </section>
        </aside>

        <div className="work-area">
          <nav className="view-tabs" aria-label="Workspace views">
            {([
              ["overview", "Evidence state", Database],
              ["sources", "Sources", FolderGit2],
              ["search", "Search", Search],
              ["investigate", "Investigate", Sparkles],
              ["runs", "Workflow", Workflow],
            ] as const).map(([id, label, Icon]) => <button key={id} className={activeView === id ? "active" : ""} onClick={() => setActiveView(id)}><Icon size={16} /> {label}</button>)}
            <button className="refresh-workspace" onClick={refreshProject} disabled={!projectId || busy === "refresh-project"}><RefreshCw size={15} className={busy === "refresh-project" ? "spin" : ""} /> Refresh</button>
          </nav>

          {activeView === "overview" && <section className="view-stack">
            <div className="section-lead"><div><p className="kicker">Project evidence state</p><h2>{selectedProject?.name ?? "No project selected"}</h2><p>Readiness is intentionally conservative. Missing runtime material is not treated as a healthy system.</p></div>{readiness ? <div className={`grade ${statusTone(readiness.grade === "excellent" ? "completed" : readiness.grade === "empty" ? "failed" : "running")}`}><strong>{readiness.score}</strong><span>{readiness.grade}</span></div> : null}</div>
            {readiness ? <>
              <article className="summary-card"><p>{readiness.summary}</p><div className="metric-strip"><span><b>{readiness.counts.sources}</b> sources</span><span><b>{readiness.counts.documents}</b> documents</span><span><b>{readiness.counts.chunks}</b> chunks</span><span><b>{readiness.latest_sync?.parser_errors ?? 0}</b> parser errors</span></div></article>
              <div className="three-grid">
                <article className="evidence-card"><h3>Evidence present</h3><div className="token-cloud">{coverageEntries.length ? coverageEntries.map(([key]) => <span key={key}>{key.replace("has_", "")}</span>) : <span>Nothing indexed yet</span>}</div></article>
                <article className="evidence-card warning"><h3>Evidence missing</h3>{readiness.missing_evidence.length ? <ul>{readiness.missing_evidence.slice(0, 4).map((item) => <li key={item}><TriangleAlert size={14} /> {item}</li>)}</ul> : <p>No high-priority gaps returned.</p>}</article>
                <article className="evidence-card"><h3>Next useful question</h3>{readiness.suggested_questions.slice(0, 2).map((question) => <button className="question-button" key={question} onClick={() => useSuggestedQuestion(question, "search")}><span>{question}</span><ChevronRight size={15} /></button>)}</article>
              </div>
            </> : <Empty icon={Database} title="Select a project to see its evidence state" detail="Readiness combines source coverage, sync health, indexed documents, and missing operational evidence." />}
          </section>}

          {activeView === "sources" && <section className="view-stack">
            <div className="section-lead"><div><p className="kicker">Evidence intake</p><h2>Register a repository with the evidence chain.</h2><p>Public GitHub URLs are stored as source configuration. A deployed Collector performs the clone, normalization, and sync; the UI never handles repository credentials.</p></div></div>
            <form className="source-form" onSubmit={addSource}>
              <div><label htmlFor="repo-url">Public GitHub repository URL</label><input id="repo-url" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://github.com/owner/repository" disabled={!projectId} /></div>
              <div><label htmlFor="source-name">Source label</label><input id="source-name" value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder="Optional: inferred from URL" disabled={!projectId} /></div>
              <button className="primary-button" type="submit" disabled={!projectId || busy === "source"}><GitBranch size={16} /> Register source</button>
            </form>
            <div className="source-list">
              {sources.length ? sources.map((source) => {
                const latest = syncs[source.id];
                return <article className="source-row" key={source.id}>
                  <div className="source-icon"><FolderGit2 size={19} /></div>
                  <div className="source-details"><div><h3>{source.name}</h3><span className="tag quiet">{source.source_type}</span></div><p>Mode: {source.sync_mode} · last sync {dateLabel(source.last_sync_finished_at)}</p></div>
                  <div className="sync-stats">{latest ? <><span className={`tag ${statusTone(latest.status)}`}>{latest.status}</span><small>{latest.documents_received} docs · {latest.chunks_created} chunks</small></> : <><span className={`tag ${statusTone(source.status)}`}>{source.status}</span><small>Awaiting first sync</small></>}</div>
                </article>;
              }) : <Empty icon={FolderGit2} title="No sources registered" detail="Add a public repository above, then configure the deployed Collector for that repository to start a real sync." />}
            </div>
          </section>}

          {activeView === "search" && <section className="view-stack split-layout">
            <form className="query-panel" onSubmit={runSearch}><p className="kicker">Cited retrieval</p><h2>Search the evidence record.</h2><p>Use implementation, config, API, or architecture questions. Results are evidence, not an answer invented around it.</p><label htmlFor="search-query">Question</label><textarea id="search-query" rows={6} value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} /><button className="primary-button" type="submit" disabled={!projectId || busy === "search"}><Search size={16} /> Search evidence</button>{searchResult ? <div className="query-facts"><span>{searchResult.total} results</span><span>{searchResult.latency_ms} ms</span><span>{searchResult.query_intent ?? "generic"}</span></div> : null}</form>
            <div className="results-panel">{searchResult ? <><div className="results-header"><div><p className="kicker">Retrieved material</p><h2>Evidence ranked for this question</h2></div><span className="tag good">{searchResult.total} found</span></div>{searchResult.results.map((item) => <article className="evidence-result" key={item.chunk_id}><div className="result-topline"><span className="source-badge">{item.source_type}</span><span>score {item.score.toFixed(3)}</span></div><h3>{item.document_path}</h3><p>{item.text_preview}</p><footer><span>{item.citation?.lines || item.citation?.location || "Citation mapped"}</span><button onClick={() => navigator.clipboard.writeText(item.document_path).then(() => setNotice("Evidence path copied."))} title="Copy evidence path"><Clipboard size={15} /></button></footer></article>)}</> : <Empty icon={FileSearch} title="Search starts with a project" detail="Choose a project with synced evidence, then ask a concrete engineering question." />}</div>
          </section>}

          {activeView === "investigate" && <section className="view-stack split-layout">
            <form className="query-panel investigation-form" onSubmit={runInvestigation}><p className="kicker">Evidence-bound investigation</p><h2>Ask what the record can support.</h2><p>IncidentOps will warn when logs, deploy context, or incident history are absent. Low confidence is an operational result, not a failure state.</p><label htmlFor="investigation-query">Investigation question</label><textarea id="investigation-query" rows={6} value={investigationQuery} onChange={(event) => setInvestigationQuery(event.target.value)} /><button className="primary-button" type="submit" disabled={!projectId || busy === "investigate"}><Sparkles size={16} /> Investigate evidence</button><button className="secondary-button" type="button" onClick={startWorkflow} disabled={!projectId || busy === "workflow"}><Play size={15} /> Start approval workflow</button></form>
            <div className="results-panel">{investigation ? <><div className="results-header"><div><p className="kicker">Investigation result</p><h2>What the evidence supports</h2></div><span className={`tag ${statusTone(investigation.confidence === "high" ? "completed" : investigation.confidence === "low" ? "running" : "failed")}`}>{investigation.confidence} confidence</span></div><article className="finding"><h3>{investigation.likely_root_cause.summary}</h3>{investigation.answer ? <p>{investigation.answer}</p> : null}{investigation.suggested_fix ? <div className="recommendation"><span>Suggested action</span><p>{investigation.suggested_fix}</p></div> : null}<div className="citation-line"><CheckCircle2 size={15} /> {investigation.citations.length} citations · {investigation.latency_ms} ms</div></article>{investigation.missing_data.length ? <article className="missing-card"><h3><ShieldAlert size={17} /> What is missing</h3>{investigation.missing_data.slice(0, 5).map((item) => <p key={item}>{item}</p>)}</article> : null}</> : <Empty icon={Sparkles} title="Investigation is evidence-first" detail="Ask a runtime question only when relevant logs, deploys, and incident material have been synced." />}</div>
          </section>}

          {activeView === "runs" && <section className="view-stack">
            <div className="section-lead"><div><p className="kicker">Approval-aware workflow</p><h2>Follow execution without guessing what is happening.</h2><p>Workflow runs preserve node events and stop at approval boundaries. No external write is performed from this screen.</p></div>{workflow ? <span className={`tag ${statusTone(workflow.status)}`}>{workflow.status}</span> : null}</div>
            {workflow ? <div className="workflow-grid"><article className="run-summary"><div className="run-symbol"><Workflow size={23} /></div><h3>{workflow.task_type || "Investigation workflow"}</h3><p>Risk level: {workflow.risk_level || "not assigned"}</p>{workflow.pending_approval ? <div className="approval-note"><TriangleAlert size={16} /> This run is waiting for a human approval decision.</div> : null}{workflow.error ? <div className="error-note"><XCircle size={16} /> {workflow.error}</div> : null}<button className="secondary-button" onClick={() => pollRun(workflow.run_id).catch((error) => setNotice(error.message))}><RefreshCw size={15} /> Refresh events</button></article><ol className="event-stream">{events.length ? events.map((event) => <li key={event.sequence_no}><span className="event-index">{String(event.sequence_no).padStart(2, "0")}</span><div><b>{event.event_type.replaceAll("_", " ")}</b><p>{event.node_name || "workflow"} · {dateLabel(event.created_at)}</p></div></li>) : <li><span className="event-index">--</span><div><b>Waiting for worker events</b><p>The queue will update this record as the run advances.</p></div></li>}</ol></div> : <Empty icon={Workflow} title="No workflow run in this browser session" detail="Start a workflow from the investigation view to see its live node events and approval state." />}
          </section>}
        </div>
      </section>
      <footer className="page-footer"><span>IncidentOps · evidence workspace</span><span>Every conclusion should point back to a source.</span></footer>
    </main>
  );
}

function Empty({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return <div className="empty-state"><Icon size={25} /><div><h3>{title}</h3><p>{detail}</p></div></div>;
}
