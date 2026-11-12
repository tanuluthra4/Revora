import logging
import os
import secrets
import sqlite3

import click
from flask import Flask, Response, g, render_template, request

from config import DB_PATH, Config
from database.backup import BackupError, backup_database, default_backup_path, pre_migration_backup
from database.db import get_db_connection, transaction
from database.migrations import LATEST_VERSION, run_migrations
from routes.auth import auth_bp
from routes.helpers import check_csrf, csrf_token, load_current_user
from routes.main import main_bp
from routes.projects import projects_bp
from routes.public import public_bp
from routes.reviewers import reviewers_bp
from routes.reviews import reviews_bp
from routes.tasks import tasks_bp

log = logging.getLogger(__name__)


def init_db(database_path=None):
    """Create/upgrade the database. Safe to run any number of times.

    An existing database is copied (consistently, and verified) before any
    pending migration runs. If that copy cannot be made, nothing is migrated.
    """
    path = database_path or DB_PATH
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            current = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        if current < LATEST_VERSION:
            copy = pre_migration_backup(path, LATEST_VERSION)
            log.warning("Database backed up to %s before migrating v%s -> v%s.",
                        copy, current, LATEST_VERSION)
    conn = get_db_connection(database_path)
    try:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "database", "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            conn.executescript(schema_file.read())
        conn.execute("PRAGMA journal_mode = WAL")
        return run_migrations(conn)
    finally:
        conn.close()


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    if not app.config["SECRET_KEY"]:
        if app.config["ENV"] == "production":
            raise RuntimeError("SECRET_KEY must be set when REVORA_ENV=production.")
        # Development/test only: sessions reset on every restart.
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        log.warning("SECRET_KEY is not set; using a temporary random key.")

    for blueprint in (main_bp, auth_bp, projects_bp, tasks_bp, public_bp, reviewers_bp, reviews_bp):
        app.register_blueprint(blueprint)

    app.before_request(load_current_user)
    app.before_request(check_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Everything is served from this origin; inline styles are used, inline scripts are not.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'")
        # Only the landing page is indexable; every account/proof page is private.
        if request.path not in ("/", "/robots.txt") and not request.path.startswith("/static/"):
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    @app.route("/robots.txt")
    def robots_txt():
        body = "User-agent: *\nAllow: /$\nDisallow: /\n"
        return Response(body, mimetype="text/plain")

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("brand/favicon.ico")

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user")}

    def _error_page(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(error):
        return _error_page(400, "Request rejected", getattr(error, "description", None)
                           or "The request could not be processed.")

    @app.errorhandler(403)
    def forbidden(error):
        return _error_page(403, "Not allowed", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def not_found(error):
        return _error_page(404, "Not found", "That page does not exist, or it is not yours to see.")

    @app.errorhandler(413)
    def too_large(error):
        return _error_page(413, "Request too large", "That request was too large.")

    @app.errorhandler(500)
    def server_error(error):
        # Details go to the log, never to the user.
        return _error_page(500, "Something went wrong", "An unexpected error occurred.")

    @app.cli.command("init-db")
    def init_db_command():
        """Create or upgrade the database."""
        version = init_db(app.config["DATABASE"])
        click.echo(f"Database ready (schema version {version}).")

    @app.cli.command("backup-db")
    @click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None,
                  help="Where to write the copy (default: backups/ next to the database).")
    def backup_db_command(out_path):
        """Write a consistent, verified copy of the database (safe while the app runs)."""
        source = app.config["DATABASE"]
        try:
            dest = backup_database(source, out_path or default_backup_path(source))
        except BackupError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Backup written and verified: {dest} ({os.path.getsize(dest)} bytes).")

    @app.cli.command("set-role")
    @click.argument("email")
    @click.argument("role", type=click.Choice(["developer", "reviewer"]))
    def set_role_command(email, role):
        """Grant or revoke the reviewer role for an account."""
        with transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET role = ? WHERE lower(email) = ?", (role, email.strip().lower())
            )
        click.echo(f"{cursor.rowcount} account updated.")

    init_db(app.config["DATABASE"])
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
