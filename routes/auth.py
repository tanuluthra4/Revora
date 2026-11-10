import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_db_connection, transaction
from services import login_throttle
from routes.helpers import start_session, valid_email, valid_username

auth_bp = Blueprint("auth", __name__)


ROLES = ("developer", "reviewer")


def _selected_role():
    role = request.values.get("role", "developer").strip().lower()
    if role not in ROLES:
        abort(400)
    return role


def _home_for(role):
    return url_for("tasks.review_queue") if role == "reviewer" else url_for("main.dashboard")


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    selected_role = _selected_role()
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not valid_username(username):
            error = "Username must be 3-30 characters: letters, numbers, '.', '_' or '-'."
        elif not valid_email(email):
            error = "Enter a valid email address."
        elif not (8 <= len(password) <= 128):
            error = "Password must be between 8 and 128 characters."
        else:
            try:
                with transaction() as conn:
                    taken = conn.execute(
                        "SELECT username, email FROM users WHERE lower(email) = ? OR username = ?",
                        (email, username),
                    ).fetchone()
                    if taken:
                        error = ("Email already exists" if taken["email"].lower() == email
                                 else "Username already taken")
                    else:
                        cursor = conn.execute(
                            "INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, ?)",
                            (username, email, generate_password_hash(password), selected_role),
                        )
                        user_id = cursor.lastrowid
                        conn.execute(
                            "INSERT INTO profiles (user_id, full_name) VALUES (?, ?)",
                            (user_id, username),
                        )
            except sqlite3.IntegrityError:
                error = "That username or email is already registered."
            else:
                if error is None:
                    start_session({"id": user_id, "username": username})
                    return redirect(_home_for(selected_role))

    return render_template("signup.html", error=error, selected_role=selected_role)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # The chosen portal must match the account's stored role; it never grants one.
    selected_role = _selected_role()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        client = request.remote_addr

        if login_throttle.is_blocked(client, email):
            error = "Too many failed sign-in attempts. Wait 15 minutes and try again."
            return render_template("login.html", error=error, selected_role=selected_role), 429

        conn = get_db_connection()
        try:
            user = conn.execute(
                "SELECT id, username, email, password, role FROM users WHERE lower(email) = ?",
                (email,),
            ).fetchone()
        finally:
            conn.close()

        if user and check_password_hash(user["password"], password):
            if user["role"] != selected_role:
                if user["role"] == "reviewer":
                    error = "This is a reviewer account. Please use the Reviewer login."
                else:
                    error = "This is a developer account. Please use the Developer login."
            else:
                login_throttle.record_success(client, email)
                start_session(user)
                return redirect(_home_for(user["role"]))
        else:
            login_throttle.record_failure(client, email)
            error = "Invalid email or password"

    return render_template("login.html", error=error, selected_role=selected_role)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
