from __future__ import annotations

import atexit
import functools
import secrets
import time
from typing import Any, Callable, TypeVar

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from waitress import serve

from .config import Settings, load_settings
from .context import DpmContext
from .projects import ProjectError
from .security import hash_password, verify_password
from .utils import utc_now

F = TypeVar("F", bound=Callable[..., Any])
DPM_VERSION = "1.0.0"
DPM_NAME = "Deploy Project Manager"


def _ensure_admin(context: DpmContext) -> None:
    count = context.db.fetchone("SELECT COUNT(*) AS count FROM users")["count"]
    if count:
        return
    settings = context.settings
    now = utc_now()
    context.db.execute(
        """
        INSERT INTO users (username, password_hash, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            settings.admin_username if settings.admin_password_hash else "admin",
            settings.admin_password_hash or hash_password("admin"),
            1 if settings.admin_is_default or not settings.admin_password_hash else 0,
            now,
            now,
        ],
    )


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> Flask:
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
        return context.db.fetchone("SELECT * FROM users WHERE id = ?", [user_id]) if user_id else None

    def authorized_api() -> bool:
        supplied = request.headers.get("X-DPM-Token", "")
        return bool(supplied and secrets.compare_digest(supplied, settings.cli_token)) or current_user() is not None

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
            "dpm_name": DPM_NAME,
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
            return redirect(url_for("dashboard")) if current_user() else render_template("login.html", error=None)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = context.db.fetchone("SELECT * FROM users WHERE username = ?", [username])
        if not user or not verify_password(password, user["password_hash"]):
            time.sleep(0.35)
            return render_template("login.html", error="Неверный логин или пароль", entered_username=username), 401
        session.clear()
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(24)
        next_url = request.args.get("next")
        return redirect(next_url) if next_url and next_url.startswith(prefix + "/") else redirect(url_for("dashboard"))

    @app.route(prefix + "/logout", methods=["POST"])
    @page_login_required
    def logout() -> Response:
        session.clear()
        return redirect(url_for("login"))

    @app.route(prefix + "/")
    @page_login_required
    def dashboard() -> str:
        return render_template("dashboard.html", active_page="projects")

    @app.route(prefix + "/projects/<int:project_id>")
    @page_login_required
    def project_page(project_id: int) -> str:
        try:
            project = context.projects.get_project(project_id)
        except ProjectError:
            abort(404)
        return render_template("project.html", active_page="projects", project_id=project_id, project_name=project["name"])

    @app.route(prefix + "/projects/<int:project_id>/services/<service_name>")
    @page_login_required
    def service_page(project_id: int, service_name: str) -> str:
        try:
            project, service = context.projects.service(project_id, service_name)
        except ProjectError:
            abort(404)
        return render_template(
            "service.html",
            active_page="projects",
            project_id=project_id,
            project_name=project["name"],
            service_name=service["name"],
        )

    @app.route(prefix + "/api/health")
    def api_health() -> Any:
        return jsonify({"ok": True, "name": DPM_NAME, "version": DPM_VERSION})

    @app.route(prefix + "/api/session")
    @api_login_required
    def api_session() -> Any:
        user = current_user()
        return jsonify({"ok": True, "user": {"username": user["username"], "is_default": bool(user["is_default"])} if user else None, "version": DPM_VERSION})

    @app.route(prefix + "/api/dashboard")
    @api_login_required
    def api_dashboard() -> Any:
        projects = context.projects.list_projects()
        services = [service for project in projects for service in project["services"]]
        return jsonify({
            "ok": True,
            "projects": projects,
            "stats": {
                "projects": len(projects),
                "services": len(services),
                "ready": sum(1 for service in services if service["status"] in {"running", "healthy", "completed"}),
                "attention": sum(1 for project in projects if project["actual_state"] in {"failed", "degraded"}),
                "deploying": sum(1 for project in projects if project["deploying"]),
            },
        })

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
                name=str(payload["name"]) if payload.get("name") else None,
                compose_file=str(payload.get("compose_file", "compose.yml")),
                env_file=str(payload["env_file"]) if payload.get("env_file") else None,
                compose_project_name=str(payload["compose_project_name"]) if payload.get("compose_project_name") else None,
                auto_update=bool(payload.get("auto_update", True)),
                poll_interval=int(payload.get("poll_interval") or settings.poll_interval),
            )
            return jsonify({"ok": True, "project": project}), 202
        except (ProjectError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>", methods=["GET", "PATCH", "DELETE"])
    @api_login_required
    def api_project(project_id: int) -> Any:
        try:
            if request.method == "GET":
                return jsonify({"ok": True, "project": context.projects.get_project(project_id)})
            if request.method == "PATCH":
                return jsonify({"ok": True, "project": context.projects.update_project(project_id, request.get_json(silent=True) or {})})
            context.projects.delete_project(project_id, purge=True)
            return jsonify({"ok": True})
        except (ProjectError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/<action>", methods=["POST"])
    @api_login_required
    def api_project_action(project_id: int, action: str) -> Any:
        try:
            if action == "check":
                accepted = context.projects.schedule_check(project_id)
                return jsonify({"ok": True, "accepted": accepted}), 202
            if action == "deploy":
                accepted = context.projects.schedule_deploy(project_id, force=False, reason="manual deploy")
                return jsonify({"ok": True, "accepted": accepted}), 202
            if action == "redeploy":
                accepted = context.projects.schedule_deploy(project_id, force=True, reason="manual redeploy")
                return jsonify({"ok": True, "accepted": accepted}), 202
            if action == "start":
                return jsonify({"ok": True, "project": context.projects.start_project(project_id)})
            if action == "stop":
                return jsonify({"ok": True, "project": context.projects.stop_project(project_id)})
            raise ProjectError("Unknown project action")
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/logs")
    @api_login_required
    def api_project_logs(project_id: int) -> Any:
        try:
            lines = min(2000, max(20, int(request.args.get("lines", 300))))
            return jsonify({"ok": True, "logs": context.projects.latest_deploy_log(project_id, lines)})
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.route(prefix + "/api/projects/<int:project_id>/services/<service_name>")
    @api_login_required
    def api_service(project_id: int, service_name: str) -> Any:
        try:
            project, service = context.projects.service(project_id, service_name)
            return jsonify({"ok": True, "project": project, "service": service})
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.route(prefix + "/api/projects/<int:project_id>/services/<service_name>/<action>", methods=["POST"])
    @api_login_required
    def api_service_action(project_id: int, service_name: str, action: str) -> Any:
        try:
            service = context.projects.service_action(project_id, service_name, action)
            return jsonify({"ok": True, "service": service})
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route(prefix + "/api/projects/<int:project_id>/services/<service_name>/logs")
    @api_login_required
    def api_service_logs(project_id: int, service_name: str) -> Any:
        try:
            lines = min(2000, max(20, int(request.args.get("lines", 300))))
            return jsonify({"ok": True, "logs": context.projects.service_logs(project_id, service_name, lines)})
        except ProjectError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.route(prefix + "/api/account/password", methods=["POST"])
    @api_login_required
    def api_change_password() -> Any:
        user = current_user()
        if not user:
            return jsonify({"ok": False, "error": "Use config.sh for CLI accounts"}), 400
        payload = request.get_json(silent=True) or {}
        if not verify_password(str(payload.get("current_password", "")), user["password_hash"]):
            return jsonify({"ok": False, "error": "Current password is incorrect"}), 400
        new_password = str(payload.get("new_password", ""))
        if len(new_password) < 8:
            return jsonify({"ok": False, "error": "New password must contain at least 8 characters"}), 400
        context.db.update("users", user["id"], {"password_hash": hash_password(new_password), "is_default": 0, "updated_at": utc_now()})
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
    print(f"{DPM_NAME} listening on {settings.host}:{settings.port}{settings.base_path}")
    serve(app, host=settings.host, port=settings.port, threads=8, channel_timeout=120)


if __name__ == "__main__":
    main()
