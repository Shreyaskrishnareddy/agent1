"""Apply orchestration: acquire jobs, run platform scripts, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, pre-filters them, launches the browser, routes to the
correct platform script, and updates the database with results.
"""

import atexit
import logging
import signal
import sys
import platform
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from agent1 import config
from agent1.database import get_connection
from agent1.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals, clear_events,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = config.DEFAULTS["poll_interval"]

_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, company_name
                FROM jobs
                WHERE (url = ? OR url LIKE ?)
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, like)).fetchone()
        else:
            row = conn.execute("""
                SELECT url, title, site, company_name
                FROM jobs
                WHERE (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                ORDER BY discovered_at DESC
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]]).fetchone()

        if not row:
            conn.rollback()
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?
            WHERE url = ?
        """, (now, duration_ms, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL "
        "WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried."""
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "sso_required", "site_blocked",
    "skippable_platform:linkedin",
}


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return result in PERMANENT_FAILURES or reason in PERMANENT_FAILURES


# ---------------------------------------------------------------------------
# Platform routing
# ---------------------------------------------------------------------------

def _get_applicant(platform: str, browser, profile, resume_text, resume_pdf, job):
    """Return the correct platform applicant based on ATS detection."""
    from agent1.platforms.greenhouse import GreenhouseApplicant
    from agent1.platforms.lever import LeverApplicant
    from agent1.platforms.ashby import AshbyApplicant

    PLATFORM_MAP = {
        "greenhouse": GreenhouseApplicant,
        "lever": LeverApplicant,
        "ashby": AshbyApplicant,
    }

    cls = PLATFORM_MAP.get(platform)
    if cls is None:
        return None

    return cls(browser, profile, resume_text, resume_pdf, job)


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, worker_id: int = 0,
            headless: bool = False, dry_run: bool = False,
            skip_filter: bool = False) -> tuple[str, int]:
    """Apply to a single job.

    Returns:
        Tuple of (status_string, duration_ms).
    """
    start = time.time()
    job_title = job.get("title") or "Unknown"
    job_site = job.get("company_name") or job.get("site", "")

    update_state(worker_id, status="filtering", job_title=job_title,
                 company=job_site, start_time=time.time(), actions=0,
                 last_action="pre-filtering")

    # 1. Pre-filter (no browser needed)
    if not skip_filter:
        from agent1.filter import prefilter_job
        add_event(f"[W{worker_id}] Filtering: {job_title[:40]}")
        result = prefilter_job(job["url"])

        if not result.eligible:
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] FILTERED ({elapsed}s): {result.reason}")
            update_state(worker_id, status="failed",
                         last_action=f"filtered: {result.reason[:25]}")
            duration_ms = int((time.time() - start) * 1000)
            return f"failed:{result.reason}", duration_ms

        platform = result.platform
    else:
        from agent1.platforms.detector import detect_platform
        platform = detect_platform(job["url"])

    # 2. Load profile and resume
    profile = config.load_profile()
    resume_text = ""
    if config.RESUME_PATH.exists():
        resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    resume_pdf = str(config.RESUME_PDF_PATH)

    # 3. Check if we have a script for this platform
    applicant_cls_available = platform in ("greenhouse", "lever", "ashby")

    if not applicant_cls_available:
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] SKIP ({elapsed}s): no script for '{platform}'")
        update_state(worker_id, status="failed",
                     last_action=f"no script: {platform}")
        duration_ms = int((time.time() - start) * 1000)
        return f"failed:unsupported_platform:{platform}", duration_ms

    # 4. Launch browser and run platform script
    from agent1.browser import Browser

    update_state(worker_id, status="applying", last_action="launching browser")
    add_event(f"[W{worker_id}] Applying: {job_title[:40]} @ {job_site}")

    try:
        with Browser(headless=headless, worker_id=worker_id) as browser:
            applicant = _get_applicant(
                platform, browser, profile, resume_text, resume_pdf, job
            )

            update_state(worker_id, last_action="filling form",
                         actions=1)

            status = applicant.apply()

            if dry_run and status == "applied":
                status = "applied"  # Still mark as applied in dry run for testing
                add_event(f"[W{worker_id}] DRY RUN: would have submitted")

    except Exception as e:
        logger.exception("Worker %d apply error", worker_id)
        status = f"failed:{str(e)[:100]}"

    duration_ms = int((time.time() - start) * 1000)
    elapsed = int(time.time() - start)

    # 5. Update dashboard
    if status == "applied":
        add_event(f"[W{worker_id}] APPLIED ({elapsed}s): {job_title[:30]}")
        update_state(worker_id, status="applied",
                     last_action=f"APPLIED ({elapsed}s)")
    elif status == "expired":
        add_event(f"[W{worker_id}] EXPIRED ({elapsed}s): {job_title[:30]}")
        update_state(worker_id, status="expired",
                     last_action=f"EXPIRED ({elapsed}s)")
    else:
        reason = status.split(":", 1)[-1] if ":" in status else status
        add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
        update_state(worker_id, status="failed",
                     last_action=f"FAILED: {reason[:25]}")

    return status, duration_ms


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                headless: bool = False, dry_run: bool = False,
                auto: bool = False, skip_filter: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty."""
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0

    # Stagger worker launches
    if worker_id > 0:
        stagger = worker_id * 10
        add_event(f"[W{worker_id}] Waiting {stagger}s (staggered start)...")
        update_state(worker_id, status="idle", last_action=f"stagger {stagger}s")
        if _stop_event.wait(timeout=stagger):
            update_state(worker_id, status="done", last_action="stopped")
            return 0, 0

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break
            continue

        empty_polls = 0

        try:
            result, duration_ms = run_job(
                job, worker_id=worker_id, headless=headless,
                dry_run=dry_run, skip_filter=skip_filter,
            )

            if result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d error", worker_id)
            add_event(f"[W{worker_id}] Error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def reset_state() -> None:
    """Reset module state between batch iterations."""
    _stop_event.clear()
    clear_events()


def main(limit: int = 1, target_url: str | None = None,
         headless: bool = False, dry_run: bool = False,
         continuous: bool = False, poll_interval: int = 60,
         workers: int = 1, auto: bool = False,
         skip_filter: bool = False) -> None:
    """Launch the apply pipeline."""
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label})...")
    console.print("[dim]Ctrl+C = stop[/dim]")

    def _sigint_handler(sig, frame):
        console.print("\n[red bold]STOPPING[/red bold]")
        _stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    headless=headless,
                    dry_run=dry_run,
                    auto=auto,
                    skip_filter=skip_filter,
                )
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=effective_limit,
                            target_url=target_url,
                            headless=headless,
                            dry_run=dry_run,
                            auto=auto,
                            skip_filter=skip_filter,
                        ): i
                        for i in range(workers)
                    }

                    results = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
