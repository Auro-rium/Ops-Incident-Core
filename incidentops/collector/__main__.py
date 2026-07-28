from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from incidentops.config.settings import Settings

from .service import CollectorService
from .benchmark import CoreBenchmarkClient


def main() -> None:
    parser = argparse.ArgumentParser(prog="incidentops-collector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "sync"):
        command = subparsers.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--max-files", type=int)
    sync = subparsers.choices["sync"]
    sync.add_argument("--project-id", required=True)
    sync.add_argument("--source-name", required=True)
    sync.add_argument("--collector-name", default="incidentops-collector")
    sync.add_argument("--environment", default="production")
    sync.add_argument("--batch-size", type=int, default=50)
    sync.add_argument("--repo-name")
    sync.add_argument("--branch")
    sync.add_argument("--commit-sha")
    daemon = subparsers.add_parser("daemon")
    collector_root = os.getenv("COLLECTOR_ROOT", "").strip()
    daemon.add_argument("--path", type=Path, default=Path(collector_root) if collector_root else None)
    daemon.add_argument("--interval-seconds", type=int, default=300)
    daemon.add_argument("--repo-url", default=os.getenv("COLLECTOR_REPO_URL", ""))
    daemon.add_argument("--project-id", default=os.getenv("INCIDENTOPS_PROJECT_ID", ""))
    daemon.add_argument("--source-name", default=os.getenv("SOURCE_NAME", "collector-source"))
    daemon.add_argument("--collector-name", default=os.getenv("COLLECTOR_NAME", "incidentops-collector"))
    daemon.add_argument("--environment", default=os.getenv("COLLECTOR_ENVIRONMENT", "production"))
    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--repo-url", required=True)
    benchmark.add_argument("--project-id", required=True)
    benchmark.add_argument("--source-name", required=True)
    benchmark.add_argument("--max-files", type=int, default=1500)
    benchmark.add_argument("--batch-size", type=int, default=100)
    benchmark.add_argument("--changed-file-target", default="README.md")
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument("--include-path", action="append", default=[])
    benchmark.add_argument("--exclude-path", action="append", default=[])
    benchmark.add_argument("--query", action="append", default=[])
    args = parser.parse_args()
    service = CollectorService(Settings())
    if args.command == "inspect":
        summary = service.inspect(args.path, args.max_files)
    elif args.command == "sync":
        summary = service.sync(
            args.path, project_id=args.project_id, source_name=args.source_name,
            collector_name=args.collector_name, environment=args.environment,
            batch_size=args.batch_size, max_files=args.max_files, repo_name=args.repo_name,
            branch=args.branch, commit_sha=args.commit_sha,
        )
        print(json.dumps(summary.diagnostics(), sort_keys=True))
        return
    elif args.command == "daemon":
        if (not args.path and not args.repo_url) or not args.project_id:
            raise SystemExit("COLLECTOR_ROOT or COLLECTOR_REPO_URL and INCIDENTOPS_PROJECT_ID are required for daemon mode")
        while True:
            workspace = Path(tempfile.mkdtemp(prefix="incidentops-collector-daemon-"))
            try:
                root = args.path
                if args.repo_url:
                    root = workspace / "repo"
                    subprocess.run(["git", "clone", "--depth", "1", args.repo_url, str(root)], check=True)
                summary = service.sync(
                    root, project_id=args.project_id, source_name=args.source_name,
                    collector_name=args.collector_name, environment=args.environment,
                    repo_name=Path(args.repo_url.rstrip("/")).stem if args.repo_url else None,
                )
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
            print(json.dumps(summary.diagnostics(), sort_keys=True), flush=True)
            time.sleep(args.interval_seconds)
    else:
        workspace = Path(tempfile.mkdtemp(prefix="incidentops-collector-"))
        try:
            checkout = workspace / "repo"
            subprocess.run(["git", "clone", "--depth", "1", args.repo_url, str(checkout)], check=True)
            summary = service.sync(
                checkout, project_id=args.project_id, source_name=args.source_name,
                collector_name="incidentops-benchmark", environment="azure",
                batch_size=args.batch_size, max_files=args.max_files,
                repo_name=Path(args.repo_url.rstrip("/")).stem,
                include_paths=args.include_path or None,
                exclude_paths=args.exclude_path or None,
            )
            repeat_summary = service.sync(
                checkout, project_id=args.project_id, source_name=args.source_name,
                collector_name="incidentops-benchmark", environment="azure",
                batch_size=args.batch_size, max_files=args.max_files,
                repo_name=Path(args.repo_url.rstrip("/")).stem,
                include_paths=args.include_path or None,
                exclude_paths=args.exclude_path or None,
            )
            changed = checkout / args.changed_file_target
            changed_file_update_detected = False
            changed_summary = None
            if changed.is_file():
                with changed.open("a", encoding="utf-8") as handle:
                    handle.write("\n<!-- incidentops benchmark mutation; do not commit -->\n")
                changed_summary = service.sync(
                    checkout, project_id=args.project_id, source_name=args.source_name,
                    collector_name="incidentops-benchmark", environment="azure",
                    batch_size=args.batch_size, max_files=args.max_files,
                    repo_name=Path(args.repo_url.rstrip("/")).stem,
                    include_paths=args.include_path or None,
                    exclude_paths=args.exclude_path or None,
                )
                changed_file_update_detected = changed_summary.documents_updated > 0
            benchmark_client = CoreBenchmarkClient(
                os.environ["INCIDENTOPS_API_URL"], os.environ["INCIDENTOPS_TOKEN"]
            )
            try:
                latest_sync = benchmark_client.latest_sync(summary.source_id or "")
                integrity = benchmark_client.source_integrity(args.project_id, summary.source_id or "")
                searches = []
                for query in args.query:
                    response = benchmark_client.search(args.project_id, query)
                    results = response.get("results") or []
                    searches.append(
                        {
                            "query": query,
                            "search_result_count": int(response.get("total", len(results)) or 0),
                            "query_intent": response.get("query_intent"),
                            "top_evidence_paths": [item.get("document_path") for item in results[:5]],
                            "latency_ms": response.get("latency_ms"),
                            "source_type_distribution": (response.get("debug") or {}).get("source_type_distribution", {}),
                        }
                    )
            finally:
                benchmark_client.close()
            report = {
                "benchmark": True,
                "repo_url": args.repo_url,
                **summary.diagnostics(),
                "repeat_sync_skipped_unchanged": repeat_summary.documents_unchanged,
                "changed_file_path": args.changed_file_target,
                "changed_file_update_detected": changed_file_update_detected,
                "changed_sync_documents": changed_summary.documents_synced if changed_summary else 0,
                "latest_core_sync_status": latest_sync.get("status"),
                "latest_core_sync_diagnostics": latest_sync.get("diagnostics", {}),
                "duplicate_chunks_after_update": integrity.get("duplicate_chunk_rows"),
                "duplicate_chunk_groups_after_update": integrity.get("duplicate_chunk_groups"),
                "search_queries": searches,
                "search_pass": bool(searches) and all(item["search_result_count"] > 0 for item in searches),
            }
            if args.output:
                args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps(report, sort_keys=True))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return
    print(json.dumps(summary.diagnostics(), sort_keys=True))


if __name__ == "__main__":
    main()
