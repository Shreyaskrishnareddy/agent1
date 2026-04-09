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


@app.command()
def apply(
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()
    console.print("[yellow]Apply command not yet implemented. Coming in Phase 6.[/yellow]")


@app.command()
def batch(
    file: Optional[Path] = typer.Argument(None, help="File with job URLs (one per line)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel browser workers."),
    auto: bool = typer.Option(False, "--auto", help="Auto-continue without prompting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
) -> None:
    """Batch-apply to jobs."""
    _bootstrap()
    console.print("[yellow]Batch command not yet implemented. Coming in Phase 6.[/yellow]")


# ---------------------------------------------------------------------------
# Bootstrap helper
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Load env, create dirs, ensure DB exists."""
    from agent1.config import load_env, ensure_dirs
    from agent1.database import init_db
    load_env()
    ensure_dirs()
    init_db()
