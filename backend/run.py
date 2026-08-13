"""
run.py — Entry point. Run with:  python run.py

Loads .env if python-dotenv is available (see requirements.txt) so the
DBPILOT_APP_PG_* variables in db.py are populated without exporting
them by hand every time.
"""

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from platform_api import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
