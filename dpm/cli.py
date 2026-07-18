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

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-DPM-Token": self.token,
                "User-Agent": "dpm-cli/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except (ValueError, UnicodeDecodeError):
                message = str(exc)
            raise CliError(message) from exc
        except urllib.error.URLError as exc:
            raise CliError(f"Cannot connect to DPM daemon: {exc.reason}") from exc


def print_services(services: list[dict[str, Any]]) -> None:
    if not services:
        print("No services")
        return
    rows = [
        (
            str(service["id"]),
            f"{service['project_name']}/{service['name']}",
            service["status"].upper(),
            str(service.get("pid") or "-"),
            (service.get("deployed_commit") or "-")[:8],
        )
        for service in services
    ]
    header = ["ID", "SERVICE", "STATUS", "PID", "COMMIT"]
    widths = [
        max(len(row[index]) for row in (header, *rows))
        for index in range(len(header))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dpm", description="Dimka Project Manager CLI")
    subparsers = parser.add_subparsers(dest="group", required=True)
    subparsers.add_parser("status", help="Show all services")

    project = subparsers.add_parser("project", help="Project operations")
    project_sub = project.add_subparsers(dest="action", required=True)
    project_sub.add_parser("list")
    add = project_sub.add_parser("add")
    add.add_argument("repository_url")
    add.add_argument("--branch", default="master")
    add.add_argument("--name")
    add.add_argument("--no-auto-update", action="store_true")
    check = project_sub.add_parser("check")
    check.add_argument("project_id", type=int)
    deploy = project_sub.add_parser("deploy")
    deploy.add_argument("project_id", type=int)
    remove = project_sub.add_parser("remove")
    remove.add_argument("project_id", type=int)
    remove.add_argument("--yes", action="store_true")

    service = subparsers.add_parser("service", help="Service operations")
    service_sub = service.add_subparsers(dest="action", required=True)
    service_sub.add_parser("list")
    for action in ("start", "stop", "restart", "delete"):
        command = service_sub.add_parser(action)
        command.add_argument("service_id", type=int)

    logs = subparsers.add_parser("logs", help="Show service logs")
    logs.add_argument("service_id", type=int)
    logs.add_argument("--lines", type=int, default=200)
    logs.add_argument("--follow", "-f", action="store_true")

    args = parser.parse_args(argv)
    client = DpmClient()

    try:
        if args.group == "status":
            data = client.request("GET", "/dashboard")
            print_services(data["services"])
            stats = data["stats"]
            print(
                f"\n{stats['running']}/{stats['services']} running, "
                f"{stats['failed']} failed, {stats['deploying']} deploying"
            )
            return 0

        if args.group == "project":
            if args.action == "list":
                data = client.request("GET", "/projects")
                for item in data["projects"]:
                    print(
                        f"{item['id']:>3}  {item['name']:<28} "
                        f"{item['deploy_status']:<12} {item['branch']:<16} "
                        f"{(item.get('deployed_commit') or '-')[:10]}"
                    )
            elif args.action == "add":
                data = client.request(
                    "POST",
                    "/projects",
                    {
                        "repository_url": args.repository_url,
                        "branch": args.branch,
                        "name": args.name,
                        "auto_update": not args.no_auto_update,
                    },
                )
                print(f"Project queued: {data['project']['name']} (id={data['project']['id']})")
            elif args.action in {"check", "deploy"}:
                client.request("POST", f"/projects/{args.project_id}/{args.action}", {})
                print(f"Project {args.action} queued")
            elif args.action == "remove":
                if not args.yes:
                    answer = input(f"Delete project {args.project_id} and its working copy? [y/N] ")
                    if answer.lower() not in {"y", "yes"}:
                        return 1
                client.request("DELETE", f"/projects/{args.project_id}")
                print("Project removed")
            return 0

        if args.group == "service":
            if args.action == "list":
                data = client.request("GET", "/services")
                print_services(data["services"])
            elif args.action == "delete":
                client.request("DELETE", f"/services/{args.service_id}")
                print("Service deleted")
            else:
                data = client.request(
                    "POST", f"/services/{args.service_id}/{args.action}", {}
                )
                item = data["service"]
                print(f"{item['project_name']}/{item['name']}: {item['status']}")
            return 0

        if args.group == "logs":
            previous = None
            while True:
                query = urllib.parse.urlencode({"lines": args.lines})
                data = client.request("GET", f"/services/{args.service_id}/logs?{query}")
                text = data.get("logs", "")
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
