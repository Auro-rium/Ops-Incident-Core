"use client";

import { useMemo, useState } from "react";

type SearchHit = {
  document_path: string;
  source_type: string;
  text_preview: string;
};

type RunEvent = {
  sequence_no: number;
  event_type: string;
  node_name?: string | null;
};

export default function HomePage() {
  const [apiBaseUrl, setApiBaseUrl] = useState("http://127.0.0.1:8000");
  const [projectName, setProjectName] = useState("local-project");
  const [projectId, setProjectId] = useState("");
  const [dataPath, setDataPath] = useState("/path/to/logs-and-docs");
  const [query, setQuery] = useState("Why did latency increase after the last deploy?");
  const [status, setStatus] = useState("");
  const [evidence, setEvidence] = useState<SearchHit[]>([]);
  const [investigation, setInvestigation] = useState<Record<string, unknown> | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);

  const sectionStyle = useMemo(
    () => ({
      background: "#ffffff",
      border: "1px solid #e5e7eb",
      borderRadius: 8,
      padding: 16,
    }),
    [],
  );

  async function createProject() {
    setStatus("Creating project...");
    const response = await fetch(`${apiBaseUrl}/v1/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: projectName, demo_mode: true }),
    });
    const payload = await response.json();
    setProjectId(payload.project_id ?? "");
    setStatus(response.ok ? "Project created." : payload.detail ?? "Project creation failed.");
  }

  async function ingestPath() {
    if (!projectId) {
      setStatus("Create a project first.");
      return;
    }
    setStatus("Ingesting path...");
    const response = await fetch(`${apiBaseUrl}/v1/projects/${projectId}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: dataPath }),
    });
    const payload = await response.json();
    setStatus(
      response.ok
        ? `Ingested ${payload.documents_ingested} documents and ${payload.chunks_created} chunks.`
        : payload.detail ?? "Ingestion failed.",
    );
  }

  async function runSearch() {
    if (!projectId) {
      setStatus("Create a project first.");
      return;
    }
    setStatus("Searching...");
    const response = await fetch(`${apiBaseUrl}/v1/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, query, top_k: 8 }),
    });
    const payload = await response.json();
    setEvidence(payload.results ?? []);
    setStatus(response.ok ? "Search complete." : payload.detail ?? "Search failed.");
  }

  async function runInvestigation() {
    if (!projectId) {
      setStatus("Create a project first.");
      return;
    }
    setStatus("Investigating...");
    const response = await fetch(`${apiBaseUrl}/v1/investigate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, query, top_k: 8 }),
    });
    const payload = await response.json();
    setInvestigation(payload);
    setStatus(response.ok ? "Investigation complete." : payload.detail ?? "Investigation failed.");
  }

  async function createRun() {
    if (!projectId) {
      setStatus("Create a project first.");
      return;
    }
    setStatus("Creating workflow run...");
    const response = await fetch(`${apiBaseUrl}/v1/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, query, top_k: 8, create_issue_draft: true }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setStatus(payload.detail ?? "Run creation failed.");
      return;
    }
    const eventsResponse = await fetch(`${apiBaseUrl}/v1/runs/${payload.run_id}/events`);
    const eventsPayload = await eventsResponse.json();
    setRunEvents(eventsPayload ?? []);
    setStatus("Workflow run created.");
  }

  return (
    <main style={{ padding: 24, maxWidth: 1200, margin: "0 auto", display: "grid", gap: 16 }}>
      <section style={{ display: "grid", gap: 8 }}>
        <h1 style={{ margin: 0, fontSize: 28 }}>IncidentOps Agent</h1>
        <p style={{ margin: 0, color: "#4b5563" }}>
          Bring your own logs, docs, code, incident reports, and deploy metadata.
        </p>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Connection</h2>
        <div style={{ display: "grid", gap: 10 }}>
          <input value={apiBaseUrl} onChange={(e) => setApiBaseUrl(e.target.value)} />
          <input value={projectName} onChange={(e) => setProjectName(e.target.value)} />
          <button onClick={createProject}>Create Project</button>
          <div>Project ID: {projectId || "not created"}</div>
        </div>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Ingestion</h2>
        <div style={{ display: "grid", gap: 10 }}>
          <input value={dataPath} onChange={(e) => setDataPath(e.target.value)} />
          <button onClick={ingestPath}>Ingest Server-Visible Path</button>
        </div>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Investigation</h2>
        <div style={{ display: "grid", gap: 10 }}>
          <textarea value={query} onChange={(e) => setQuery(e.target.value)} rows={3} />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button onClick={runSearch}>Search Evidence</button>
            <button onClick={runInvestigation}>Investigate</button>
            <button onClick={createRun}>Create Workflow Run</button>
          </div>
          <div style={{ color: "#4b5563" }}>{status}</div>
        </div>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Evidence</h2>
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {evidence.map((item) => (
            <li key={`${item.document_path}-${item.source_type}`}>
              <strong>{item.document_path}</strong> [{item.source_type}]<br />
              <span style={{ color: "#4b5563" }}>{item.text_preview}</span>
            </li>
          ))}
        </ul>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Investigation Result</h2>
        <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
          {investigation ? JSON.stringify(investigation, null, 2) : "No investigation run yet."}
        </pre>
      </section>

      <section style={sectionStyle}>
        <h2 style={{ marginTop: 0, fontSize: 18 }}>Run Events</h2>
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {runEvents.map((event) => (
            <li key={`${event.sequence_no}-${event.event_type}`}>
              {event.sequence_no}. {event.event_type} {event.node_name ? `(${event.node_name})` : ""}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
