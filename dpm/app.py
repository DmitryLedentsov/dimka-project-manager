from __future__ import annotations

import atexit
import functools
import secrets
import time
from typing import Any, Callable, TypeVar

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from waitress import serve

from .components import ComponentError
from .config import Settings, load_settings
from .context import DpmContext
from .projects import ProjectError
from .security import hash_password, verify_password
from .utils import utc_now

F = TypeVar("F", bound=Callable[..., Any])
DPM_VERSION = "0.2.0"


def _ensure_admin(context: DpmContext) -> None:
    settings = context.settings
    count = context.db.fetchone("SELECT COUNT(*) AS count FROM users")["count"]
    if count:
        return
    now = utc_now()
    username = settings.admin_username if settings.admin_password_hash else "admin"
    password_hash = settings.admin_password_hash or hash_password("admin")
    context.db.execute(
        """
        INSERT INTO users (username, password_hash, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            username,
            password_hash,
            1 if settings.admin_is_default or not settings.admin_password_hash else 0,
            now,
            now,
        ],
    )


def create_app(
    settings: Settings | None = None,
    *,
    start_background: bool = True,
) -> Flask:
    settings = settings or load_settings()
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
        static_url_path=f"{settings.base_path}/static",
    )
    app.secret_key = settings.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=settings.secure_cookie,
        SESSION_COOKIE_PATH=settings.base_path,
        MAX_CONTENT_LENGTH=256 * 1024,
    )

    context = DpmContext.create(settings, start_background=start_background)
    app.extensions["dpm"] = context
    _ensure_admin(context)
    atexit.register(context.shutdown)
    prefix = settings.base_path

    def current_user() -> dict[str, Any] | None:
        user_id = session.get("user_id")
        if not user_id:
            return None
        return context.db.fetchone("SELECT * FROM users WHERE id = ?", [user_id])

    def authorized_api() -> bool:
        supplied = request.headers.get("X-DPM-Token", "")
        if supplied and secrets.compare_digest(supplied, settings.cli_token):
            return True
        return current_user() is not None

    def csrf_valid() -> bool:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return True
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        return bool(supplied and expected and secrets.compare_digest(supplied, expected))

    def page_login_required(view: F) -> F:
        @functools.wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not current_user():
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def api_login_required(view: F) -> F:
        @functools.wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not authorized_api():
                return jsonify({"ok": False, "error": "Authentication required"}), 401
            if current_user() and not csrf_valid():
                return jsonify({"ok": False, "error": "Invalid CSRF token"}), 403
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    @app.context_processor
    def template_context() -> dict[str, Any]:
        user = current_user()
        if user and "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(24)
        return {
            "base_path": prefix,
            "public_url": settings.public_url,
            "current_user": user,
            "csrf_token": session.get("csrf_token", ""),
            "default_credentials": bool(user and user.get("is_default")),
            "dpm_version": DPM_VERSION,
        }

    @app.route("/")
    def root() -> Response:
        return redirect(prefix + "/")

    @app.route(prefix)
    def prefix_root() -> Response:
        return redirect(prefix + "/")

    @app.route(prefix + "/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "GET":
            if current_user():
                return redirect(url_for("dashboard"))
            return render_template("login.html", error=None)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = context.db.fetchone("SELECT * FROM users WHERE username = ?", [username])
        if not user or not verify_password(password, user["password_hash"]):
            time.sleep(0.35)
            return (
                render_template(
                    "login.html",
                    error="Неверный логин или пароль",
                    entered_username=username,
                ),
                401,
            )
        session.clear()
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(24)
        next_url = request.args.get("next")
        if next_url and next_url.startswith(prefix + "/"):
            return redirect(next_url)
        return redirect(url_for("dashboard"))

    @app.route(prefix + "/logout", methods=["POST"])
    @page_login_required
    def logout() -> Response:
        session.clear()
        return redirect(url_for("login"))

    @app.route(prefix + "/")
    @page_login_required
    def dashboard() -> str:
        return render_template("dashboard.html", active_page="overview")

    @app.route(prefix + "/projects/<int:project_id>")
    @page_login_required
    def project_page(project_id: int) -> str:
        try:
            project = context.projects.get_project(project_id)
        except ProjectError:
            abort(404)
        return render_template(
            "project.html",
            active_page="overview",
            project_id=project_id,
            project_name=project["name"],
        )

    @app.route(prefix + "/components/<int:component_id>")
    @page_login_required
    def component_page(component_id: int) -> str:
        try:
            component = context.components.get_component(component_id)
            template = context.components.component_template(component_id)
        except ComponentError:
            abort(404)
        return render_template(
            template,
            active_page="overview",
            component_id=component_id,
            component_name=component["name"],
            project_name=component["project_name"],
            component_type=component["component_type"],
        )

    @app.route(prefix + "/services/<int:service_id>")
    @page_login_required
    def legacy_service_page(service_id: int) -> Response:
        return redirect(f"{prefix}/components/{service_id}")

    @app.route(prefix + "/api/session")
    @api_login_required
    def api_session() -> Any:
        user = current_user()
        return jsonify(
            {
                "ok": True,
                "user": {
                    "username": user["username"],
                    "is_default": bool(user["is_default"]),
                }
                if user
                else None,
                "version": DPM_VERSION,
            }
        )

    @app.route(prefix + "/api/dashboard")
    @api_login_required
    def api_dashboard() -> Any:
        projects = context.projects.list_projects()
        components = [component for project in projects for component in project["components"]]
        stats = {
            "projects": len(projects),
            "components": len(components),
            "ready": sum(
                1
                for component in components
                if component["status"] in {"running", "ready"}
            ),
            "attention": sum(
                1
                for project in projects
                if project["actual_state"] in {"failed", "degraded"}
            ),
            "deploying": sum(1 for project in projects if project["deploying"]),
        }
        issues = [
            project
            for project in projects
            if project["actual_state"] in {"failed", "degraded"}
        ]
        return jsonify(
            {
                "ok": True,
                "stats": stats,
                "projects": projects,
                "components": components,
                "services": components,
                "issues": issues,
            }
        )

    @app.route(prefix + "/api/projects", methods=["GET", "POST"])
    @api_login_required
    def api_projects() -> Any:
        if request.method == "GET":
            return jsonify({"ok": True, "projects": context.projects.list_projects()})
        payload = request.get_json(silent=True) or {}
        try:
            project = context.projects.add_project(
                repository_url=str(payload.get("repository_url", "")),
                branch=str(payload.get("branch", "master")),
                name=(str(payload["name"]) if payload.get("name") else None),
                auto_update=bool(payload.get("auto_update", True)),
                poll_interval=int(payload.get("poll_interval") or settings.poll_interval),
            )
            return jsonify({"ok": True, "project": project}), 202
        except (ProjectError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>", methods=["GET", "DELETE", "PATCH"])
    @api_login_required
    def api_project(project_id: int) -> Any:
        try:
            if request.method == "GET":
                return jsonify({"ok": True, "project": context.projects.get_project(project_id)})
            if request.method == "DELETE":
                context.projects.delete_project(project_id, purge=True)
                return jsonify({"ok": True})
            payload = request.get_json(silent=True) or {}
            values: dict[str, Any] = {"updated_at": utc_now()}
            if "auto_update" in payload:
                values["auto_update"] = 1 if payload["auto_update"] else 0
            if "poll_interval" in payload:
                values["poll_interval"] = max(15, int(payload["poll_interval"]))
            context.db.update("projects", project_id, values)
            return jsonify({"ok": True, "project": context.projects.get_project(project_id)})
        except (ProjectError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/check", methods=["POST"])
    @api_login_required
    def api_project_check(project_id: int) -> Any:
        context.projects.get_project(project_id)
        accepted = context.projects.schedule_check(project_id)
        return jsonify({"ok": True, "accepted": accepted}), 202

    @app.route(prefix + "/api/projects/<int:project_id>/deploy", methods=["POST"])
    @api_login_required
    def api_project_deploy(project_id: int) -> Any:
        context.projects.get_project(project_id)
        accepted = context.projects.schedule_deploy(
            project_id, reason="manual deploy", force=False
        )
        return jsonify({"ok": True, "accepted": accepted}), 202

    @app.route(prefix + "/api/projects/<int:project_id>/redeploy", methods=["POST"])
    @api_login_required
    def api_project_redeploy(project_id: int) -> Any:
        context.projects.get_project(project_id)
        accepted = context.projects.schedule_deploy(
            project_id, reason="manual redeploy", force=True
        )
        return jsonify({"ok": True, "accepted": accepted}), 202

    @app.route(prefix + "/api/projects/<int:project_id>/start", methods=["POST"])
    @api_login_required
    def api_project_start(project_id: int) -> Any:
        try:
            return jsonify(
                {"ok": True, "project": context.projects.start_project(project_id)}
            )
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/stop", methods=["POST"])
    @api_login_required
    def api_project_stop(project_id: int) -> Any:
        try:
            return jsonify(
                {"ok": True, "project": context.projects.stop_project(project_id)}
            )
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/logs")
    @api_login_required
    def api_project_logs(project_id: int) -> Any:
        lines = min(2000, max(20, int(request.args.get("lines", 250))))
        return jsonify(
            {"ok": True, "logs": context.projects.latest_deploy_log(project_id, lines)}
        )

    @app.route(prefix + "/api/components")
    @api_login_required
    def api_components() -> Any:
        project_id = request.args.get("project_id")
        return jsonify(
            {
                "ok": True,
                "components": context.components.list_components(
                    int(project_id) if project_id else None
                ),
            }
        )

    @app.route(prefix + "/api/components/<int:component_id>")
    @api_login_required
    def api_component(component_id: int) -> Any:
        try:
            component = context.components.get_component(component_id)
            project = context.projects.get_project(int(component["project_id"]))
            return jsonify({"ok": True, "component": component, "project": project})
        except (ComponentError, ProjectError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    def component_action(component_id: int, action: str) -> Any:
        try:
            if action == "start":
                component = context.components.start_component(component_id)
            elif action == "stop":
                component = context.components.stop_component(component_id)
            elif action == "restart":
                component = context.components.restart_component(component_id)
            else:
                raise ComponentError("Unknown component action")
            return jsonify({"ok": True, "component": component})
        except ComponentError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/components/<int:component_id>/start", methods=["POST"])
    @api_login_required
    def api_component_start(component_id: int) -> Any:
        return component_action(component_id, "start")

    @app.route(prefix + "/api/components/<int:component_id>/stop", methods=["POST"])
    @api_login_required
    def api_component_stop(component_id: int) -> Any:
        return component_action(component_id, "stop")

    @app.route(prefix + "/api/components/<int:component_id>/restart", methods=["POST"])
    @api_login_required
    def api_component_restart(component_id: int) -> Any:
        return component_action(component_id, "restart")

    @app.route(prefix + "/api/components/<int:component_id>", methods=["DELETE"])
    @api_login_required
    def api_component_delete(component_id: int) -> Any:
        try:
            context.components.delete_component(component_id)
            return jsonify({"ok": True})
        except ComponentError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.route(prefix + "/api/components/<int:component_id>/logs")
    @api_login_required
    def api_component_logs(component_id: int) -> Any:
        try:
            lines = min(2000, max(20, int(request.args.get("lines", 250))))
            payload = context.components.component_logs(component_id, lines)
            return jsonify({"ok": True, **payload})
        except ComponentError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    # Compatibility API for existing DPM CLI clients.
    @app.route(prefix + "/api/services")
    @api_login_required
    def api_services() -> Any:
        components = context.components.list_components()
        return jsonify({"ok": True, "services": components, "components": components})

    @app.route(prefix + "/api/services/<int:service_id>")
    @api_login_required
    def api_service(service_id: int) -> Any:
        return api_component(service_id)

    @app.route(prefix + "/api/services/<int:service_id>/start", methods=["POST"])
    @api_login_required
    def api_service_start(service_id: int) -> Any:
        return component_action(service_id, "start")

    @app.route(prefix + "/api/services/<int:service_id>/stop", methods=["POST"])
    @api_login_required
    def api_service_stop(service_id: int) -> Any:
        return component_action(service_id, "stop")

    @app.route(prefix + "/api/services/<int:service_id>/restart", methods=["POST"])
    @api_login_required
    def api_service_restart(service_id: int) -> Any:
        return component_action(service_id, "restart")

    @app.route(prefix + "/api/services/<int:service_id>/logs")
    @api_login_required
    def api_service_logs(service_id: int) -> Any:
        return api_component_logs(service_id)

    @app.route(prefix + "/api/account/password", methods=["POST"])
    @api_login_required
    def api_change_password() -> Any:
        user = current_user()
        if not user:
            return jsonify({"ok": False, "error": "Use config.sh for CLI accounts"}), 400
        payload = request.get_json(silent=True) or {}
        current = str(payload.get("current_password", ""))
        new_password = str(payload.get("new_password", ""))
        if not verify_password(current, user["password_hash"]):
            return jsonify({"ok": False, "error": "Current password is incorrect"}), 400
        if len(new_password) < 8:
            return jsonify({"ok": False, "error": "New password must contain at least 8 characters"}), 400
        context.db.update(
            "users",
            user["id"],
            {
                "password_hash": hash_password(new_password),
                "is_default": 0,
                "updated_at": utc_now(),
            },
        )
        return jsonify({"ok": True})

    @app.errorhandler(404)
    def not_found(_: Exception) -> Any:
        if request.path.startswith(prefix + "/api/"):
            return jsonify({"ok": False, "error": "Not found"}), 404
        return render_template("error.html", code=404, message="Страница не найдена"), 404

    @app.errorhandler(500)
    def server_error(_: Exception) -> Any:
        if request.path.startswith(prefix + "/api/"):
            return jsonify({"ok": False, "error": "Internal server error"}), 500
        return render_template("error.html", code=500, message="Внутренняя ошибка"), 500

    return app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    print(f"DPM listening on {settings.public_url}")
    serve(app, host=settings.host, port=settings.port, threads=8, channel_timeout=120)


if __name__ == "__main__":
    main()
