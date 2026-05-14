from __future__ import annotations

import argparse
import json

from incidentops.ingestion.diagnostics import inspect_folder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a folder before ingestion.")
    parser.add_argument("--data-path", required=True)
    return parser


def _print_section(title: str, payload) -> None:
    print(f"{title}")
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"  {key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}")
    elif isinstance(payload, list):
        for item in payload:
            print(f"  - {json.dumps(item)}")
    else:
        print(f"  {payload}")
    print()


def main() -> int:
    args = build_parser().parse_args()
    report = inspect_folder(args.data_path)
    _print_section("Folder", report["data_path"])
    _print_section(
        "Summary",
        {
            "total_files_seen": report["total_files_seen"],
            "ingestable_files": report["ingestable_files"],
            "estimated_ingestability": report["estimated_ingestability"],
        },
    )
    _print_section("File Type Counts", report["file_type_counts"])
    _print_section("Likely Source Types", report["likely_source_types"])
    _print_section("Source Coverage", report["source_coverage"])
    _print_section("Oversized Files", report["oversized_files"])
    _print_section("Unsupported Files", report["unsupported_files"])
    _print_section("Warnings", report["warnings"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
