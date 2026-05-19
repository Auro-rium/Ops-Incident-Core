from __future__ import annotations

import argparse
import json
from pathlib import Path


READ_ONLY_TASK_KEYS = {
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render an ECS task definition with a new image.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--command", nargs="*", help="Optional replacement container command.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    task_definition = json.loads(args.input.read_text(encoding="utf-8"))
    for key in READ_ONLY_TASK_KEYS:
        task_definition.pop(key, None)

    updated = False
    for container in task_definition.get("containerDefinitions", []):
        if container.get("name") != args.container:
            continue
        container["image"] = args.image
        if args.command:
            container["command"] = args.command
        updated = True
        break

    if not updated:
        raise SystemExit(f"container not found in task definition: {args.container}")

    args.output.write_text(json.dumps(task_definition, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
