"""Agent1 configuration: paths, defaults, user data."""

import json
import os
from pathlib import Path

# User data directory
APP_DIR = Path(os.environ.get("AGENT1_DIR", Path.home() / ".agent1"))

# Core paths
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
DB_PATH = APP_DIR / "agent1.db"
ENV_PATH = APP_DIR / ".env"

# Generated output
LOG_DIR = APP_DIR / "logs"

# Browser profiles (Playwright persistent contexts)
BROWSER_PROFILE_DIR = APP_DIR / "browser-profiles"

# Worker working directories
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Defaults
DEFAULTS = {
    "max_apply_attempts": 3,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
    "gemma_model": "gemma-4-26b-a4b-it",
}


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, LOG_DIR, BROWSER_PROFILE_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.agent1/profile.json."""
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `agent1 init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_env():
    """Load environment variables from ~/.agent1/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()
