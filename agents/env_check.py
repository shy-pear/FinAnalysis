"""Environment loading and API-key checks — shared by every entry point.

The orchestrator (Stage 4) and dashboard chat (Stage 5) call `get_required()`
instead of reading os.environ directly, so a missing or placeholder key always
produces an actionable message rather than a stack trace deep in the SDK.

Run directly to verify your .env is being read:

    python agents/env_check.py

Secret values are never printed — only a masked fingerprint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# name -> (is_secret, what it is / where to get it)
REQUIRED_VARS = {
    "ANTHROPIC_API_KEY": (
        True,
        "powers the agent pipeline — create one at https://console.anthropic.com/settings/keys",
    ),
    "SEC_EDGAR_USER_AGENT": (
        False,
        "descriptive User-Agent for SEC EDGAR requests — your name/app + contact email",
    ),
}

# Values copied verbatim from .env.example count as not configured
PLACEHOLDER_FRAGMENTS = ("your-key-here", "your-email@example.com")


def load_env() -> None:
    """Load .env from the project root (no-op if the file doesn't exist)."""
    load_dotenv(ENV_FILE)


def _is_set(value: str | None) -> bool:
    if not value or not value.strip():
        return False
    return not any(frag in value for frag in PLACEHOLDER_FRAGMENTS)


def get_required(name: str) -> str:
    """Return the value of a required env var, or exit with setup instructions."""
    load_env()
    value = os.environ.get(name)
    if _is_set(value):
        return value
    _, hint = REQUIRED_VARS.get(name, (True, ""))
    lines = [
        f"Missing configuration: {name} is not set{' (or still the placeholder)' if value else ''}.",
        f"  {name}: {hint}",
        "",
        "To fix:",
        f"  1. cp .env.example .env        (if you haven't already; .env is gitignored)",
        f"  2. Edit .env and set the {name}=... line to your real value",
        f"  3. Verify with: python agents/env_check.py",
    ]
    raise SystemExit("\n".join(lines))


def _mask(value: str) -> str:
    """Fingerprint a secret without revealing it."""
    if len(value) <= 12:
        return "(set, hidden)"
    return f"{value[:7]}…{value[-4:]}"


def main() -> int:
    load_env()
    print(f".env file: {ENV_FILE} ({'found' if ENV_FILE.exists() else 'NOT FOUND'})\n")
    ok = True
    for name, (is_secret, hint) in REQUIRED_VARS.items():
        value = os.environ.get(name)
        if _is_set(value):
            shown = _mask(value) if is_secret else value
            print(f"  OK       {name} = {shown}")
        else:
            ok = False
            state = "placeholder" if value else "missing"
            print(f"  {state.upper():<8} {name} — {hint}")
    print()
    if ok:
        print("Environment is configured. The pipeline can run.")
    else:
        print("Not configured yet: cp .env.example .env, edit the flagged lines, re-run this check.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
