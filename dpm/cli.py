from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import load_settings


class CliError(RuntimeError):
    pass


class DpmClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.base_url = f"http://127.0.0.1:{settings.port}{settings.base_path}/api"
        self.token = settings.cli_token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "X-DPM-Token": self.token, "User-Agent": "dpm-cli/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except (ValueError, UnicodeDecodeError):
                message = str(exc)
            raise CliError(message) from exc
        except urllib.error.URLError as exc:
            raise CliError(f"Cannot connect to DPM daemon: {exc.reason}") from exc


def print_projects(projects: list[dict[str, Any]]) -> None:
    if not projects:
        print("No projects")
        return
    print("ID   PROJECT                       STATE       READY  COMMIT      COMPOSE")
    print("---  ----------------------------  ----------  -----  ----------  ----------------")
    for item in projects:
        print(
            f"{item['id']:>3}  {item['name']:<28}  {item['actual_state']:<10}  "
            f"{item['ready_count']:>2}/{item['service_count']:<2}  "
            f"{(item.get('deployed_commit') or '-')[:10]:<10}  {item.get('compose_file') or 'compose.yml'}"
        )


def print_services(project: dict[str, Any]) -> None:
    services = project.get("services") or []
    if not services:
        print("No Compose services")
        return
    print("SERVICE                   STATUS       STATE          IMAGE")
    print("------------------------  -----------  -------------  --------------------------------")
    for item in services:
        print(f"{item['name']:<24}  {item['status']:<11}  {item['state']:<13}  {item['image']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dpm", description="Deploy Project Manager CLI")
    groups = parser.add_subparsers(dest="group", required=True)
    groups.add_parser("status")

    project = groups.add_parser("project")
    project_actions = project.add_subparsers(dest="action", required=True)
    project_actions.add_parser("list")
    add = project_actions.add_parser("add")
    add.add_argument("repository_url")
    add.add_argument("--branch", default="master")
    add.add_argument("--name")
    add.add_argument("--compose-file", default="compose.yml")
    add.add_argument("--env-file")
    add.add_argument("--compose-project-name")
    add.add_argument("--no-auto-update", action="store_true")
    for action in ("show", "check", "deploy", "redeploy", "start", "stop"):
        command = project_actions.add_parser(action)
        command.add_argument("project_id", type=int)
    remove = project_actions.add_parser("remove")
    remove.add_argument("project_id", type=int)
    remove.add_argument("--yes", action="store_true")

    service = groups.add_parser("service")
    service_actions = service.add_subparsers(dest="action", required=True)
    service_list = service_actions.add_parser("list")
    service_list.add_argument("project_id", type=int)
    for action in ("start", "stop", "restart"):
        command = service_actions.add_parser(action)
        command.add_argument("project_id", type=int)
        command.add_argument("service_name")

    logs = groups.add_parser("logs")
    logs.add_argument("project_id", type=int)
    logs.add_argument("service_name", nargs="?")
    logs.add_argument("--lines", type=int, default=300)
    logs.add_argument("--follow", "-f", action="store_true")

    args = parser.parse_args(argv)
    client = DpmClient()
    try:
        if args.group == "status":
            data = client.request("GET", "/dashboard")
            print_projects(data["projects"])
            stats = data["stats"]
            print(f"\n{stats['projects']} projects, {stats['ready']}/{stats['services']} services ready, {stats['attention']} need attention")
            return 0

        if args.group == "project":
            if args.action == "list":
                print_projects(client.request("GET", "/projects")["projects"])
            elif args.action == "add":
                data = client.request("POST", "/projects", {
                    "repository_url": args.repository_url,
                    "branch": args.branch,
                    "name": args.name,
                    "compose_file": args.compose_file,
                    "env_file": args.env_file,
                    "compose_project_name": args.compose_project_name,
                    "auto_update": not args.no_auto_update,
                })
                print(f"Project queued: {data['project']['name']} (id={data['project']['id']})")
            elif args.action == "show":
                project_data = client.request("GET", f"/projects/{args.project_id}")["project"]
                print_projects([project_data])
                print()
                print_services(project_data)
            elif args.action in {"check", "deploy", "redeploy", "start", "stop"}:
                client.request("POST", f"/projects/{args.project_id}/{args.action}", {})
                print(f"Project {args.action} accepted")
            elif args.action == "remove":
                if not args.yes:
                    answer = input(f"Delete project {args.project_id}, its containers and checkout? [y/N] ")
                    if answer.lower() not in {"y", "yes"}:
                        return 1
                client.request("DELETE", f"/projects/{args.project_id}")
                print("Project removed; named volumes were preserved")
            return 0

        if args.group == "service":
            if args.action == "list":
                project_data = client.request("GET", f"/projects/{args.project_id}")["project"]
                print_services(project_data)
            else:
                path = f"/projects/{args.project_id}/services/{urllib.parse.quote(args.service_name, safe='')}/{args.action}"
                data = client.request("POST", path, {})
                print(f"{data['service']['name']}: {data['service']['status']}")
            return 0

        if args.group == "logs":
            previous = None
            while True:
                if args.service_name:
                    path = f"/projects/{args.project_id}/services/{urllib.parse.quote(args.service_name, safe='')}/logs?lines={args.lines}"
                else:
                    path = f"/projects/{args.project_id}/logs?lines={args.lines}"
                text = client.request("GET", path).get("logs", "")
                if text != previous:
                    if args.follow and previous is not None:
                        print("\033[2J\033[H", end="")
                    print(text)
                    previous = text
                if not args.follow:
                    break
                time.sleep(2)
            return 0
    except CliError as exc:
        print(f"dpm: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
