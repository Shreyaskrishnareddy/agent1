"""Agent1 CLI — the main entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from agent1 import __version__

app = typer.Typer(
    name="agent1",
    help="Agent1 — free, open-source job application automation.",
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]agent1[/bold] {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Agent1 — free, open-source job application automation."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume)."""
    from agent1.wizard.init import run_wizard

    run_wizard()


@app.command()
def load(
    file: Path = typer.Argument(..., help="Path to a file with job URLs (one per line)"),
    site: str = typer.Option("manual", help="Source site label"),
    strategy: str = typer.Option("file_import", help="Discovery strategy label"),
) -> None:
    """Bulk-import job URLs from a text file into the database."""
    if not file.exists():
        console.print(f"[red]Error:[/red] File not found: {file}")
        raise typer.Exit(code=1)

    urls: list[str] = []
    skipped = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not (line.startswith("http://") or line.startswith("https://")):
            console.print(f"[yellow]Warning:[/yellow] Skipping invalid URL: {line}")
            skipped += 1
            continue
        urls.append(line)

    if not urls:
        console.print("[yellow]No valid URLs found in file.[/yellow]")
        raise typer.Exit(code=1)

    from agent1.database import init_db, get_connection, store_jobs

    init_db()
    conn = get_connection()
    jobs = [{"url": url} for url in urls]
    new_count, dup_count = store_jobs(conn, jobs, site, strategy)

    console.print(
        f"Loaded [green]{new_count}[/green] new jobs "
        f"([yellow]{dup_count}[/yellow] duplicates skipped)"
    )
    if skipped:
        console.print(f"[yellow]{skipped}[/yellow] invalid lines were skipped")


@app.command()
def discover(
    repo: str = typer.Option(
        "jobright-ai/2026-Software-Engineer-New-Grad",
        "--repo", "-r",
        help="GitHub repo to fetch jobs from (owner/name).",
    ),
    site: str = typer.Option("jobright", help="Source site label."),
    strategy: str = typer.Option("github_discover", help="Discovery strategy label."),
    keep_utm: bool = typer.Option(False, "--keep-utm", help="Keep UTM tracking params in URLs."),
    no_filter: bool = typer.Option(False, "--no-filter", help="Disable default US+Remote location filter."),
    location: Optional[str] = typer.Option(
        None, "--location", help="Only import jobs matching this location substring.",
    ),
    work_model: Optional[str] = typer.Option(
        None, "--work-model", help="Filter by work model: Remote, Hybrid, 'On Site'.",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-l", help="Max number of new jobs to import.",
    ),
    source: str = typer.Option("all", "--source", "-s", help="Source: all, jobright, speedyapply."),
) -> None:
    """Discover jobs from GitHub repos and import into the database."""
    _bootstrap()

    from agent1.discover import discover_all_jobs, is_us_location, VALID_SOURCES
    if source not in VALID_SOURCES:
        console.print(f"[red]Error:[/red] --source must be one of: {', '.join(VALID_SOURCES)}")
        raise typer.Exit(code=1)
    from agent1.database import get_connection, store_jobs

    console.print("[bold blue]Fetching jobs from all sources...[/bold blue]")

    try:
        jobs, source_counts = discover_all_jobs(strip_utm=not keep_utm, source=source)
    except ConnectionError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1)

    if not jobs:
        console.print("[yellow]No jobs found.[/yellow]")
        raise typer.Exit(code=1)

    for src, count in source_counts.items():
        console.print(f"  {src}: [cyan]{count}[/cyan] jobs")
    console.print(f"Total: [cyan]{len(jobs)}[/cyan] jobs (after dedup)")

    if not no_filter and not location:
        before = len(jobs)
        jobs = [j for j in jobs if is_us_location(j.get("location", ""))]
        console.print(
            f"US + Remote filter: [cyan]{len(jobs)}[/cyan] of {before} matched"
        )

    if location:
        before = len(jobs)
        lf = location.lower()
        jobs = [j for j in jobs if lf in (j.get("location") or "").lower()]
        console.print(
            f"Location filter '{location}': [cyan]{len(jobs)}[/cyan] of {before} matched"
        )

    if work_model:
        before = len(jobs)
        wf = work_model.lower()
        jobs = [j for j in jobs if wf in (j.get("work_model") or "").lower()]
        console.print(
            f"Work model filter '{work_model}': [cyan]{len(jobs)}[/cyan] of {before} matched"
        )

    if not jobs:
        console.print("[yellow]No jobs remaining after filters.[/yellow]")
        raise typer.Exit()

    conn = get_connection()

    if limit is not None and limit > 0:
        new_count = 0
        dup_count = 0
        for job in jobs:
            job_site = job.pop("_source", site)
            n, d = store_jobs(conn, [job], job_site, strategy)
            new_count += n
            dup_count += d
            if new_count >= limit:
                break
        skipped = len(jobs) - (new_count + dup_count)
        if skipped > 0:
            console.print(f"[dim]{skipped} remaining jobs not checked (limit reached)[/dim]")
    else:
        from collections import defaultdict
        by_source: dict[str, list[dict]] = defaultdict(list)
        for job in jobs:
            job_site = job.pop("_source", site)
            by_source[job_site].append(job)
        new_count = 0
        dup_count = 0
        for job_site, group in by_source.items():
            n, d = store_jobs(conn, group, job_site, strategy)
            new_count += n
            dup_count += d

    console.print(
        f"\nStored [green]{new_count}[/green] new jobs "
        f"([yellow]{dup_count}[/yellow] duplicates skipped)"
    )


@app.command()
def stats() -> None:
    """Show job pipeline statistics."""
    _bootstrap()

    from agent1.database import get_connection, get_stats

    conn = get_connection()
    s = get_stats(conn)

    table = Table(title="Agent1 Pipeline Stats")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Total jobs", str(s["total"]))
    table.add_row("Applied", f"[green]{s['applied']}[/green]")
    table.add_row("Errors", f"[red]{s['apply_errors']}[/red]")
    table.add_row("Ready to apply", f"[cyan]{s['ready_to_apply']}[/cyan]")

    console.print(table)

    if s["by_site"]:
        site_table = Table(title="Jobs by Source")
        site_table.add_column("Source")
        site_table.add_column("Count", justify="right")
        for site_name, count in s["by_site"]:
            site_table.add_row(site_name or "unknown", str(count))
        console.print(site_table)


@app.command(name="gmail-setup")
def gmail_setup() -> None:
    """Set up Gmail API access for email verification (OTP codes)."""
    from agent1.email_client import GmailClient, CREDENTIALS_PATH, TOKEN_PATH

    if not CREDENTIALS_PATH.exists():
        console.print(
            "[yellow]Gmail credentials not found.[/yellow]\n\n"
            "To set up Gmail:\n"
            "1. Go to [bold]https://console.cloud.google.com[/bold]\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the Gmail API\n"
            "4. Create OAuth 2.0 credentials (Desktop app)\n"
            f"5. Download the JSON and save it as:\n   [cyan]{CREDENTIALS_PATH}[/cyan]\n"
            "6. Run [bold]agent1 gmail-setup[/bold] again"
        )
        raise typer.Exit(code=1)

    client = GmailClient()
    if client.authenticate():
        console.print(f"[green]Gmail authenticated![/green] Token saved to {TOKEN_PATH}")

        # Quick test
        emails = client.search_emails(max_results=1, max_age_minutes=60)
        if emails:
            console.print(f"[dim]Test: found {len(emails)} recent email(s). Gmail is working.[/dim]")
        else:
            console.print("[dim]Test: no recent emails found (that's ok, Gmail is connected).[/dim]")
    else:
        console.print("[red]Gmail authentication failed.[/red]")
        raise typer.Exit(code=1)


@app.command()
def apply(
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    skip_filter: bool = typer.Option(False, "--skip-filter", help="Skip pre-filter step."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed_flag: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    # --- Utility modes (no browser needed) ---
    if mark_applied:
        from agent1.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from agent1.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed_flag:
        from agent1.apply.launcher import reset_failed
        count = reset_failed()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Pre-flight checks ---
    _preflight_checks()

    from agent1.database import get_connection
    from agent1.apply.launcher import main as apply_main

    # Check jobs ready
    if not url:
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE applied_at IS NULL "
            "AND (apply_status IS NULL OR apply_status = 'failed')"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No jobs ready to apply.[/red]\n"
                "Run [bold]agent1 load[/bold] or [bold]agent1 discover[/bold] first."
            )
            raise typer.Exit(code=1)

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:      {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:    {workers}")
    console.print(f"  Headless:   {headless}")
    console.print(f"  Dry run:    {dry_run}")
    console.print(f"  Pre-filter: {'off' if skip_filter else 'on'}")
    if url:
        console.print(f"  Target:     {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        headless=headless,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        skip_filter=skip_filter,
    )


@app.command()
def batch(
    file: Optional[Path] = typer.Argument(None, help="File with job URLs (one per line)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel browser workers."),
    auto: bool = typer.Option(False, "--auto", help="Auto-continue without prompting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    skip_filter: bool = typer.Option(False, "--skip-filter", help="Skip pre-filter step."),
    run_discover: bool = typer.Option(False, "--discover", "-d", help="Run discover before applying."),
    site: str = typer.Option("manual", help="Source site label for file import."),
    strategy: str = typer.Option("batch_import", help="Strategy label for file import."),
) -> None:
    """Batch-apply to jobs: load from file (optional), then process."""
    import signal as _signal

    _bootstrap()
    _preflight_checks()

    from agent1.config import DEFAULTS
    from agent1.database import get_connection, store_jobs
    from agent1.apply.launcher import main as apply_main, reset_state, release_lock

    if run_discover:
        discover(
            repo="jobright-ai/2026-Software-Engineer-New-Grad",
            site="jobright", strategy="github_discover",
            keep_utm=False, no_filter=False,
            location=None, work_model=None,
            limit=None, source="all",
        )

    conn = get_connection()
    max_attempts = DEFAULTS["max_apply_attempts"]

    # Load file if provided
    if file:
        if not file.exists():
            console.print(f"[red]Error:[/red] File not found: {file}")
            raise typer.Exit(code=1)

        urls: list[str] = []
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                urls.append(line)

        if urls:
            jobs = [{"url": u} for u in urls]
            new_count, dup_count = store_jobs(conn, jobs, site, strategy)
            console.print(
                f"Loaded [green]{new_count}[/green] new jobs "
                f"([yellow]{dup_count}[/yellow] duplicates skipped)"
            )

    # Count pending
    rows = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE applied_at IS NULL "
        "  AND (apply_status IS NULL OR apply_status = 'failed') "
        "  AND COALESCE(apply_attempts, 0) < ?",
        [max_attempts],
    ).fetchone()
    pending = rows[0]

    if not pending:
        console.print("[yellow]No pending jobs to apply to.[/yellow]")
        raise typer.Exit()

    console.print(f"\n[bold blue]Batch Apply[/bold blue]")
    console.print(f"  Jobs:       {pending}")
    console.print(f"  Workers:    {workers}")
    console.print(f"  Auto:       {auto}")
    console.print(f"  Headless:   {headless}")
    console.print(f"  Dry run:    {dry_run}")
    console.print()

    if auto:
        apply_main(
            limit=pending,
            headless=headless,
            dry_run=dry_run,
            continuous=False,
            workers=workers,
            auto=True,
            skip_filter=skip_filter,
        )
    else:
        # Single-job mode with prompts between
        applied = 0
        failed = 0

        for i in range(pending):
            try:
                reset_state()
                apply_main(
                    limit=1, headless=headless,
                    dry_run=dry_run, continuous=False,
                    workers=1, auto=True,
                    skip_filter=skip_filter,
                )
            except KeyboardInterrupt:
                break

            # Reset signal for clean input
            _signal.signal(_signal.SIGINT, _signal.default_int_handler)

            remaining = pending - (i + 1)
            if remaining > 0:
                console.print(f"[dim]{remaining} job(s) remaining.[/dim]")
                try:
                    input("Press Enter for next job, Ctrl+C to exit... ")
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Stopped. Run again to continue.[/yellow]")
                    break

        console.print(f"\n[bold]Batch complete.[/bold]")


# ---------------------------------------------------------------------------
# Bootstrap & preflight
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Load env, create dirs, ensure DB exists."""
    from agent1.config import load_env, ensure_dirs
    from agent1.database import init_db
    load_env()
    ensure_dirs()
    init_db()


def _preflight_checks() -> None:
    """Verify profile exists before apply/batch commands."""
    from agent1.config import PROFILE_PATH

    if not PROFILE_PATH.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]agent1 init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)
