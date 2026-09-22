"""Local-development convenience: read a .env file into the process environment.

Real environment variables always win (setdefault), so containers and Cloud Run, which set the
environment themselves and have no .env, are unaffected.
"""
import os


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, inline ' #' comments stripped; a missing file is fine."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip())
