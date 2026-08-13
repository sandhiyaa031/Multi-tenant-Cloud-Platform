"""
platform_api/__init__.py — Flask application factory.

WHY A FACTORY (create_app()) INSTEAD OF A MODULE-LEVEL `app = Flask(...)`:
it's the standard Flask pattern and it's what makes run.py trivial and
what will make automated testing possible later (a test can call
create_app() fresh for each test instead of importing one shared global
app object) — worth doing correctly from the start even though this
project won't have automated tests until a later stage.

REQUEST FLOW (applies to every route in this package):
    Browser/curl request
      -> Flask routes to the matching view function (companies.py / reads.py)
      -> view function reads query params / JSON body, does light validation
      -> view function runs a parameterized SQL statement against
         dbpilot_platform via platform_api.db.get_db()
      -> psycopg returns rows as dicts (row_factory=dict_row)
      -> view function wraps them in a JSON response with jsonify()
      -> Flask sends the JSON response back
"""

from __future__ import annotations

from flask import Flask, jsonify

from . import db as db_module
from .companies import companies_bp
from .reads import reads_bp


def create_app() -> Flask:
    app = Flask(__name__)

    app.teardown_appcontext(db_module.close_db)

    app.register_blueprint(companies_bp)
    app.register_blueprint(reads_bp)

    @app.errorhandler(404)
    def not_found(_err):
        return jsonify(error="not_found", message="No such resource."), 404

    @app.errorhandler(500)
    def server_error(_err):
        # Generic catch-all. Route-specific errors (validation, missing
        # rows, DB constraint violations) are handled and returned with
        # a proper status code inside the routes themselves — this is
        # only reached for something genuinely unexpected.
        return jsonify(error="internal_error", message="Something went wrong."), 500

    @app.get("/health")
    def health():
        """Basic liveness check — also confirms the DB connection works."""
        conn = db_module.get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return jsonify(status="ok", database="dbpilot_platform")

    return app
