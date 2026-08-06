"use client";

import {
  Activity,
  ArrowUpRight,
  Check,
  Clipboard,
  FileSearch,
  Gauge,
  Link2,
  LogIn,
  LogOut,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

type Project = { project_id: string; name: string; role?: string };
type ApiError = { detail?: string; message?: string };
type SearchResult = {
  chunk_id: string;
  document_path: string;
  source_type: string;
  score: number;
  text_preview: string;
  citation?: { label?: string; location?: string };
};
type SearchResponse = { results: SearchResult[]; total: number; latency_ms?: number; query_intent?: string };
type InvestigationResponse = {
  confidence?: string;
  likely_root_cause?: { summary?: string; confidence?: string };
  suggested_fix?: string | null;
  missing_data?: string[];
  citations?: Array<{ label?: string; location?: string }>;
  evidence?: SearchResult[];
  latency_ms?: number;
};
type ReadinessResponse = {
  score?: number;
  grade?: string;
  summary?: string;
  missing_evidence?: string[];
  suggested_questions?: string[];
  suggested_actions?: string[];
  warnings?: string[];
  counts?: { documents?: number; chunks?: number; sources?: number; failed_syncs?: number };
};

const DEFAULT_QUERY = "Where is the service configured?";
const DEFAULT_INVESTIGATION = "What evidence exists for investigating a latency regression?";

function normalizeApiUrl(value: string) {
  return value.trim().replace(/\/$/, "");
}

function errorMessage(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object") {
    const error = payload as ApiError;
    if (typeof error.detail === "string") return error.detail;
    if (typeof error.message === "string") return error.message;
  }
  return fallback;
}

export default function EvidenceDesk() {
  const [apiUrl, setApiUrl] = useState(process.env.NEXT_PUBLIC_CORE_API_URL ?? "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [health, setHealth] = useState<"unknown" | "healthy" | "unready" | "offline">("unknown");
  const [runtime, setRuntime] = useState<Record<string, unknown> | null>(null);
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [investigationQuery, setInvestigationQuery] = useState(DEFAULT_INVESTIGATION);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const [investigation, setInvestigation] = useState<InvestigationResponse | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("Connect an IncidentOps Core API to begin.");

  const connected = Boolean(normalizeApiUrl(apiUrl));
  const authenticated = Boolean(token);
  const selectedProject = projects.find((project) => project.project_id === projectId);
  const apiOrigin = useMemo(() => normalizeApiUrl(apiUrl), [apiUrl]);

  useEffect(() => {
    const saved = window.sessionStorage.getItem("incidentops.access-token");
    const savedApi = window.sessionStorage.getItem("incidentops.api-url");
    if (saved) setToken(saved);
    if (savedApi && !apiUrl) setApiUrl(savedApi);
  }, [apiUrl]);

  async function request<T>(path: string, init: RequestInit = {}, requireToken = false): Promise<T> {
    if (!apiOrigin) throw new Error("Enter an IncidentOps Core API URL first.");
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    if (requireToken && token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${apiOrigin}${path}`, { ...init, headers });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(errorMessage(payload, `Core returned HTTP ${response.status}`));
    return payload as T;
  }

  async function refreshHealth() {
    setBusy("health");
    try {
      const [healthResponse, readyResponse] = await Promise.all([
        request<{ status?: string }>("/health"),
        request<{ status?: string }>("/ready"),
      ]);
      setHealth(healthResponse.status === "ok" && readyResponse.status === "ready" ? "healthy" : "unready");
      setNotice(healthResponse.status === "ok" && readyResponse.status === "ready" ? "Core is reachable and ready." : "Core is reachable but not ready.");
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
    const previous = token;
    if (activeToken !== token) setToken(activeToken);
    try {
      const result = await request<Project[]>("/v1/projects", {}, true);
      setProjects(result);
      if (!projectId && result[0]) setProjectId(result[0].project_id);
    } finally {
      if (activeToken !== previous) setToken(previous);
    }
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("login");
    try {
      const result = await request<{ access_token: string; user: { email: string } }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(result.access_token);
      window.sessionStorage.setItem("incidentops.access-token", result.access_token);
      window.sessionStorage.setItem("incidentops.api-url", apiOrigin);
      setNotice(`Signed in as ${result.user.email}.`);
      const response = await fetch(`${apiOrigin}/v1/projects`, { headers: { Authorization: `Bearer ${result.access_token}` } });
      const loadedProjects = (await response.json()) as Project[];
      if (!response.ok) throw new Error(errorMessage(loadedProjects, "Could not load projects."));
      setProjects(loadedProjects);
      if (loadedProjects[0]) setProjectId(loadedProjects[0].project_id);
      const runtimeResponse = await fetch(`${apiOrigin}/v1/runtime/status`, { headers: { Authorization: `Bearer ${result.access_token}` } });
      if (runtimeResponse.ok) setRuntime((await runtimeResponse.json()) as Record<string, unknown>);
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
    setSearchResult(null);
    setInvestigation(null);
    setRuntime(null);
    setNotice("Signed out. Your Core token was removed from this browser session.");
  }

  async function loadReadiness() {
    if (!projectId) return;
    setBusy("readiness");
    try {
      const result = await request<ReadinessResponse>(`/v1/projects/${projectId}/readiness`, {}, true);
      setReadiness(result);
      setNotice("Readiness report refreshed.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not load readiness.");
    } finally {
      setBusy(null);
    }
  }

  async function runSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return setNotice("Choose a project before searching.");
    setBusy("search");
    try {
      const result = await request<SearchResponse>("/v1/search", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, query, top_k: 6, debug: false }),
      }, true);
      setSearchResult(result);
      setNotice(result.total ? `Found ${result.total} evidence result${result.total === 1 ? "" : "s"}.` : "No evidence matched that query.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Search failed.");
    } finally {
      setBusy(null);
    }
  }

  async function runInvestigation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return setNotice("Choose a project before investigating.");
    setBusy("investigate");
    try {
      const result = await request<InvestigationResponse>("/v1/investigate", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, query: investigationQuery, top_k: 6, debug: false }),
      }, true);
      setInvestigation(result);
      setNotice("Investigation completed with evidence and missing-data checks.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Investigation failed.");
    } finally {
      setBusy(null);
    }
  }

  async function copyQuestion(question: string) {
    await navigator.clipboard.writeText(question);
    setNotice("Question copied.");
  }

  return (
    <main>
      <header className="topbar">
        <a className="wordmark" href="#desk" aria-label="IncidentOps evidence desk">
          <span className="mark">IO</span>
          <span>IncidentOps</span>
        </a>
        <div className="connection-state">
          <span className={`pulse ${health}`} />
          <span>{health === "healthy" ? "Core ready" : health === "offline" ? "Core unavailable" : "Evidence desk"}</span>
        </div>
      </header>

      <section className="masthead" id="desk">
        <div>
          <p className="eyebrow">Engineering evidence desk</p>
          <h1>Ask the system what it can actually prove.</h1>
          <p className="lede">Connect your IncidentOps Core, choose a project, and work from cited evidence instead of a confident guess.</p>
        </div>
        <div className="signal-card" aria-label="Core connection status">
          <Link2 size={20} />
          <div><span>Endpoint</span><strong>{apiOrigin || "Not connected"}</strong></div>
          <button className="icon-button" title="Check Core status" onClick={refreshHealth} disabled={!connected || busy === "health"}>
            <RefreshCw size={17} className={busy === "health" ? "spin" : ""} />
          </button>
        </div>
      </section>

      <p className="notice" role="status">{notice}</p>

      <section className="workbench">
        <aside className="rail">
          <section className="rail-section">
            <div className="section-heading"><span>01</span><h2>Connect</h2></div>
            <label htmlFor="api-url">Core API URL</label>
            <input id="api-url" value={apiUrl} onChange={(event) => setApiUrl(event.target.value)} placeholder="https://core.example.com" inputMode="url" />
            <button className="secondary" onClick={refreshHealth} disabled={!connected || busy === "health"}>
              <Activity size={16} /> Check service
            </button>
          </section>

          <section className="rail-section">
            <div className="section-heading"><span>02</span><h2>Identity</h2></div>
            {authenticated ? (
              <>
                <p className="muted">Authenticated for this browser session.</p>
                <button className="secondary" onClick={signOut}><LogOut size={16} /> Sign out</button>
              </>
            ) : (
              <form onSubmit={signIn} className="stack">
                <label htmlFor="email">Email</label>
                <input id="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
                <label htmlFor="password">Password</label>
                <input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
                <button className="primary" type="submit" disabled={!connected || busy === "login"}><LogIn size={16} /> Sign in</button>
              </form>
            )}
          </section>

          <section className="rail-section">
            <div className="section-heading"><span>03</span><h2>Project</h2></div>
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={!authenticated}>
              <option value="">Select a project</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
            </select>
            <button className="secondary" onClick={() => loadProjects()} disabled={!authenticated || busy === "projects"}>
              <RefreshCw size={16} /> Refresh projects
            </button>
          </section>

          <section className="rail-section runtime">
            <div className="section-heading"><span>04</span><h2>Runtime</h2></div>
            {runtime ? (
              <dl>
                <div><dt>Model</dt><dd>{String(runtime.llm_provider ?? "unknown")}</dd></div>
                <div><dt>Vectors</dt><dd>{String(runtime.retrieval_backend ?? "unknown")}</dd></div>
                <div><dt>Worker</dt><dd>{String(runtime.worker_mode ?? "unknown")}</dd></div>
              </dl>
            ) : <p className="muted">Sign in to inspect runtime state.</p>}
          </section>
        </aside>

        <div className="workspace">
          <section className="panel readiness-panel">
            <div className="panel-header">
              <div><p className="eyebrow">Evidence coverage</p><h2>{selectedProject ? selectedProject.name : "Readiness"}</h2></div>
              <button className="icon-button" title="Refresh readiness" onClick={loadReadiness} disabled={!projectId || busy === "readiness"}><RefreshCw size={17} className={busy === "readiness" ? "spin" : ""} /></button>
            </div>
            {readiness ? (
              <div className="readiness-content">
                <div className="score"><strong>{readiness.score ?? "?"}</strong><span>{readiness.grade ?? "unknown"}</span></div>
                <p>{readiness.summary ?? "No readiness summary was returned."}</p>
                <div className="metric-row">
                  <span><b>{readiness.counts?.sources ?? 0}</b> sources</span>
                  <span><b>{readiness.counts?.documents ?? 0}</b> documents</span>
                  <span><b>{readiness.counts?.chunks ?? 0}</b> chunks</span>
                </div>
                {(readiness.missing_evidence?.length || readiness.warnings?.length) ? <div className="warning-list">{[...(readiness.missing_evidence ?? []), ...(readiness.warnings ?? [])].slice(0, 4).map((item) => <p key={item}><TriangleAlert size={15} /> {item}</p>)}</div> : null}
                {readiness.suggested_questions?.length ? <div className="question-list">{readiness.suggested_questions.slice(0, 3).map((item) => <button key={item} onClick={() => copyQuestion(item)} title="Copy suggested question"><Clipboard size={14} /> {item}</button>)}</div> : null}
              </div>
            ) : <div className="empty-state"><Gauge size={24} /><p>Select a project and refresh readiness to see coverage, gaps, and safe questions.</p></div>}
          </section>

          <section className="two-column">
            <form className="panel" onSubmit={runSearch}>
              <div className="panel-header"><div><p className="eyebrow">Evidence retrieval</p><h2>Search the record</h2></div><FileSearch size={20} /></div>
              <label htmlFor="search-query">Question</label>
              <textarea id="search-query" value={query} onChange={(event) => setQuery(event.target.value)} rows={4} />
              <button className="primary" type="submit" disabled={!authenticated || !projectId || busy === "search"}><Search size={16} /> Search evidence</button>
              {searchResult ? <div className="results"><p className="result-meta">{searchResult.total} results · {searchResult.latency_ms ?? "?"} ms · {searchResult.query_intent ?? "generic"}</p>{searchResult.results.map((item) => <article key={item.chunk_id}><div><span>{item.source_type}</span><b>{item.document_path || "Untitled evidence"}</b></div><p>{item.text_preview}</p><small>{item.citation?.location ?? item.citation?.label ?? "Citation available"} · score {item.score}</small></article>)}</div> : null}
            </form>

            <form className="panel" onSubmit={runInvestigation}>
              <div className="panel-header"><div><p className="eyebrow">Cautious investigation</p><h2>Assess the evidence</h2></div><Sparkles size={20} /></div>
              <label htmlFor="investigation-query">Investigation question</label>
              <textarea id="investigation-query" value={investigationQuery} onChange={(event) => setInvestigationQuery(event.target.value)} rows={4} />
              <button className="primary" type="submit" disabled={!authenticated || !projectId || busy === "investigate"}><ShieldCheck size={16} /> Investigate</button>
              {investigation ? <div className="investigation"><p className="confidence"><span>Confidence</span><b>{investigation.confidence ?? investigation.likely_root_cause?.confidence ?? "unknown"}</b></p><h3>{investigation.likely_root_cause?.summary ?? "No supported root-cause statement returned."}</h3>{investigation.suggested_fix ? <p className="fix">{investigation.suggested_fix}</p> : null}{investigation.missing_data?.length ? <div className="warning-list">{investigation.missing_data.slice(0, 4).map((item) => <p key={item}><TriangleAlert size={15} /> {item}</p>)}</div> : null}<p className="result-meta">{investigation.citations?.length ?? investigation.evidence?.length ?? 0} citations · {investigation.latency_ms ?? "?"} ms</p></div> : null}
            </form>
          </section>
        </div>
      </section>

      <footer><span>IncidentOps evidence desk</span><a href="https://github.com/Auro-rium/Ops-Incident-Core" target="_blank" rel="noreferrer">Core source <ArrowUpRight size={14} /></a></footer>
    </main>
  );
}
