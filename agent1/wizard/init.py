"""Agent1 first-time setup wizard.

Interactive flow that creates ~/.agent1/ with:
  - resume.txt (and optionally resume.pdf)
  - profile.json
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from agent1.config import (
    APP_DIR,
    DB_PATH,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    ensure_dirs,
)
from agent1.database import init_db

console = Console()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def _setup_resume() -> None:
    """Prompt for resume file and copy into APP_DIR."""
    console.print(Panel("[bold]Step 1: Resume[/bold]\nPoint to your master resume file (.txt or .pdf)."))

    has_txt = RESUME_PATH.exists()
    has_pdf = RESUME_PDF_PATH.exists()
    if has_txt or has_pdf:
        existing = []
        if has_txt:
            existing.append(str(RESUME_PATH))
        if has_pdf:
            existing.append(str(RESUME_PDF_PATH))
        console.print(f"[dim]Current resume: {', '.join(existing)}[/dim]")
        if not Confirm.ask("Replace existing resume?", default=False):
            console.print("[dim]Keeping current resume.[/dim]")
            return

    while True:
        path_str = Prompt.ask("Resume file path")
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()

        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue

        suffix = src.suffix.lower()
        if suffix not in (".txt", ".pdf"):
            console.print("[red]Unsupported format.[/red] Provide a .txt or .pdf file.")
            continue

        if suffix == ".txt":
            shutil.copy2(src, RESUME_PATH)
            console.print(f"[green]Copied to {RESUME_PATH}[/green]")
        elif suffix == ".pdf":
            shutil.copy2(src, RESUME_PDF_PATH)
            console.print(f"[green]Copied to {RESUME_PDF_PATH}[/green]")

            txt_path_str = Prompt.ask(
                "Plain-text version of your resume (.txt)",
                default="",
            )
            if txt_path_str.strip():
                txt_src = Path(txt_path_str.strip().strip('"').strip("'")).expanduser().resolve()
                if txt_src.exists():
                    shutil.copy2(txt_src, RESUME_PATH)
                    console.print(f"[green]Copied to {RESUME_PATH}[/green]")
                else:
                    console.print("[yellow]File not found, skipping plain-text copy.[/yellow]")
        break


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _setup_profile() -> dict:
    """Walk through profile questions and return a nested profile dict."""
    console.print(Panel("[bold]Step 2: Profile[/bold]\nTell Agent1 about yourself. This powers auto-fill and screening question answers."))

    existing: dict = {}
    if PROFILE_PATH.exists():
        try:
            existing = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    def _g(*keys: str, fallback: str = "") -> str:
        d = existing
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k, fallback)
            else:
                return fallback
        return d if isinstance(d, str) else fallback

    def _gb(*keys: str) -> bool:
        d = existing
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                return False
        return bool(d)

    def _gl(*keys: str) -> str:
        d = existing
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k, [])
            else:
                return ""
        return ", ".join(d) if isinstance(d, list) else ""

    profile: dict = {}

    # -- Personal --
    console.print("\n[bold cyan]Personal Information[/bold cyan]")
    full_name = Prompt.ask("Full name", default=_g("personal", "full_name") or None)
    profile["personal"] = {
        "full_name": full_name,
        "preferred_name": Prompt.ask("Preferred/nickname (leave blank to use first name)", default=_g("personal", "preferred_name")),
        "email": Prompt.ask("Email address", default=_g("personal", "email") or None),
        "phone": Prompt.ask("Phone number", default=_g("personal", "phone")),
        "city": Prompt.ask("City", default=_g("personal", "city") or None),
        "province_state": Prompt.ask("Province/State (e.g. California)", default=_g("personal", "province_state")),
        "country": Prompt.ask("Country", default=_g("personal", "country") or None),
        "postal_code": Prompt.ask("Postal/ZIP code", default=_g("personal", "postal_code")),
        "address": Prompt.ask("Street address (optional, used for form auto-fill)", default=_g("personal", "address")),
        "linkedin_url": Prompt.ask("LinkedIn URL", default=_g("personal", "linkedin_url")),
        "github_url": Prompt.ask("GitHub URL (optional)", default=_g("personal", "github_url")),
        "portfolio_url": Prompt.ask("Portfolio URL (optional)", default=_g("personal", "portfolio_url")),
        "website_url": Prompt.ask("Personal website URL (optional)", default=_g("personal", "website_url")),
        "password": Prompt.ask("Job site password (used for login walls during auto-apply)", password=True, default=_g("personal", "password")),
    }

    # -- Work Authorization --
    console.print("\n[bold cyan]Work Authorization[/bold cyan]")
    profile["work_authorization"] = {
        "legally_authorized_to_work": Confirm.ask("Are you legally authorized to work in your target country?", default=_gb("work_authorization", "legally_authorized_to_work")),
        "require_sponsorship": Confirm.ask("Will you now or in the future need sponsorship?", default=_gb("work_authorization", "require_sponsorship")),
        "work_permit_type": Prompt.ask("Work permit type (e.g. Citizen, PR, Open Work Permit — leave blank if N/A)", default=_g("work_authorization", "work_permit_type")),
    }

    # -- Compensation --
    console.print("\n[bold cyan]Compensation[/bold cyan]")
    salary = Prompt.ask("Expected annual salary (number)", default=_g("compensation", "salary_expectation"))
    salary_currency = Prompt.ask("Currency", default=_g("compensation", "salary_currency") or "USD")
    existing_min = _g("compensation", "salary_range_min")
    existing_max = _g("compensation", "salary_range_max")
    if existing_min and existing_max and existing_min != existing_max:
        default_range = f"{existing_min}-{existing_max}"
    elif existing_min:
        default_range = existing_min
    else:
        default_range = ""
    salary_range = Prompt.ask("Acceptable range (e.g. 80000-120000)", default=default_range)
    range_parts = salary_range.split("-") if "-" in salary_range else [salary, salary]
    profile["compensation"] = {
        "salary_expectation": salary,
        "salary_currency": salary_currency,
        "salary_range_min": range_parts[0].strip(),
        "salary_range_max": range_parts[1].strip() if len(range_parts) > 1 else range_parts[0].strip(),
    }

    # -- Experience --
    console.print("\n[bold cyan]Experience[/bold cyan]")
    current_title = Prompt.ask("Current/most recent job title", default=_g("experience", "current_title"))
    default_target = _g("experience", "target_role") or current_title
    target_role = Prompt.ask("Target role (what you're applying for)", default=default_target)
    profile["experience"] = {
        "years_of_experience_total": Prompt.ask("Years of professional experience", default=_g("experience", "years_of_experience_total")),
        "education_level": Prompt.ask("Highest education (e.g. Bachelor's, Master's, PhD, Self-taught)", default=_g("experience", "education_level")),
        "current_title": current_title,
        "target_role": target_role,
    }

    # -- Skills --
    console.print("\n[bold cyan]Skills[/bold cyan] (comma-separated)")
    langs = Prompt.ask("Programming languages", default=_gl("skills_boundary", "programming_languages"))
    frameworks = Prompt.ask("Frameworks & libraries", default=_gl("skills_boundary", "frameworks"))
    tools = Prompt.ask("Tools & platforms (e.g. Docker, AWS, Git)", default=_gl("skills_boundary", "tools"))
    profile["skills_boundary"] = {
        "programming_languages": [s.strip() for s in langs.split(",") if s.strip()],
        "frameworks": [s.strip() for s in frameworks.split(",") if s.strip()],
        "tools": [s.strip() for s in tools.split(",") if s.strip()],
    }

    # -- Resume Facts --
    console.print("\n[bold cyan]Resume Facts[/bold cyan]")
    console.print("[dim]These are preserved exactly during resume tailoring.[/dim]")
    companies = Prompt.ask("Companies to always keep (comma-separated)", default=_gl("resume_facts", "preserved_companies"))
    projects = Prompt.ask("Projects to always keep (comma-separated)", default=_gl("resume_facts", "preserved_projects"))
    school = Prompt.ask("School name(s) to preserve", default=_g("resume_facts", "preserved_school"))
    metrics = Prompt.ask("Real metrics to preserve (e.g. '99.9% uptime, 50k users')", default=_gl("resume_facts", "real_metrics"))
    profile["resume_facts"] = {
        "preserved_companies": [s.strip() for s in companies.split(",") if s.strip()],
        "preserved_projects": [s.strip() for s in projects.split(",") if s.strip()],
        "preserved_school": school.strip(),
        "real_metrics": [s.strip() for s in metrics.split(",") if s.strip()],
    }

    # -- EEO Voluntary --
    profile["eeo_voluntary"] = existing.get("eeo_voluntary", {
        "gender": "Decline to self-identify",
        "race_ethnicity": "Decline to self-identify",
        "veteran_status": "Decline to self-identify",
        "disability_status": "Decline to self-identify",
    })

    # -- Availability --
    profile["availability"] = {
        "earliest_start_date": Prompt.ask("Earliest start date", default=_g("availability", "earliest_start_date") or "Immediately"),
    }

    # Save
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"\n[green]Profile saved to {PROFILE_PATH}[/green]")
    return profile


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    """Run the full interactive setup wizard."""
    console.print()
    console.print(
        Panel.fit(
            "[bold green]Agent1 Setup Wizard[/bold green]\n\n"
            "This will create your configuration at:\n"
            f"  [cyan]{APP_DIR}[/cyan]\n\n"
            "You can re-run this anytime with [bold]agent1 init[/bold].",
            border_style="green",
        )
    )

    dir_existed = APP_DIR.exists()
    ensure_dirs()
    if dir_existed:
        console.print(f"[dim]Found existing config at {APP_DIR}[/dim]")
    else:
        console.print(f"[dim]Created {APP_DIR}[/dim]")

    db_existed = DB_PATH.exists()
    init_db()
    if db_existed:
        console.print("[dim]Database ready.[/dim]\n")
    else:
        console.print(f"[green]Database created at {DB_PATH}[/green]\n")

    _setup_resume()
    console.print()

    _setup_profile()
    console.print()

    console.print(
        Panel.fit(
            "[bold green]Setup complete![/bold green]\n\n"
            "Your profile and resume are saved.\n"
            "Re-run [bold]agent1 init[/bold] anytime to update.",
            border_style="green",
        )
    )
