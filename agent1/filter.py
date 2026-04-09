"""Pre-filter jobs before launching a browser.

Fetches the job page via HTTP (no browser needed) and checks for
expired listings, ineligible locations, and other disqualifiers.
Saves minutes per dead-end job.
"""

import logging
import re
from dataclasses import dataclass

import httpx

from agent1.platforms.detector import detect_platform, is_skippable_platform

logger = logging.getLogger(__name__)

# Patterns that indicate an expired/closed listing (case-insensitive)
_EXPIRED_PATTERNS = [
    r"no longer accepting applications",
    r"this position has been filled",
    r"this job is no longer available",
    r"this job has expired",
    r"job not found",
    r"position is no longer available",
    r"this listing has expired",
    r"the position you are looking for is no longer open",
    r"this role has been filled",
    r"sorry.*this job has been closed",
    r"this posting has been closed",
    r"this job posting is no longer active",
    r"application deadline has passed",
]

_EXPIRED_RE = re.compile("|".join(_EXPIRED_PATTERNS), re.IGNORECASE)

# Patterns that indicate this isn't a job application
_NOT_A_JOB_PATTERNS = [
    r"toptal\.com",
    r"turing\.com/jobs",
    r"upwork\.com",
    r"fiverr\.com",
    r"freelancer\.com",
    r"mercor\.com",
]

_NOT_A_JOB_RE = re.compile("|".join(_NOT_A_JOB_PATTERNS), re.IGNORECASE)


@dataclass
class FilterResult:
    """Result of pre-filtering a job URL."""
    eligible: bool
    reason: str       # "ok", "expired", "not_eligible_location", etc.
    platform: str     # detected ATS platform


def prefilter_job(url: str, use_ai: bool = False, profile: dict | None = None) -> FilterResult:
    """Pre-filter a job URL without launching a browser.

    Args:
        url: The job listing URL.
        use_ai: Whether to use Gemma 4 for ambiguous cases.
        profile: User profile (needed if use_ai=True).

    Returns:
        FilterResult with eligibility decision.
    """
    # 1. Detect platform
    platform = detect_platform(url)

    # 2. Skip platforms that always need login (e.g. LinkedIn)
    if is_skippable_platform(platform):
        return FilterResult(
            eligible=False,
            reason=f"skippable_platform:{platform}",
            platform=platform,
        )

    # 3. Check URL against not-a-job patterns
    if _NOT_A_JOB_RE.search(url):
        return FilterResult(
            eligible=False,
            reason="not_a_job_application",
            platform=platform,
        )

    # 4. Fetch the page via HTTP
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; agent1-bot)"},
        )
    except httpx.TimeoutException:
        logger.debug("Timeout fetching %s", url)
        # Can't determine — let it through, browser will handle it
        return FilterResult(eligible=True, reason="timeout", platform=platform)
    except httpx.HTTPError as e:
        logger.debug("HTTP error fetching %s: %s", url, e)
        return FilterResult(eligible=True, reason="http_error", platform=platform)

    # 5. Check HTTP status
    if response.status_code == 404:
        return FilterResult(eligible=False, reason="expired", platform=platform)
    if response.status_code >= 500:
        return FilterResult(eligible=True, reason="server_error", platform=platform)

    # 6. Check final URL for redirects to login/SSO
    final_url = str(response.url)
    if any(d in final_url for d in [
        "accounts.google.com", "login.microsoftonline.com",
        "okta.com", "auth0.com",
    ]):
        return FilterResult(eligible=False, reason="sso_required", platform=platform)

    # 7. Check page text for expired patterns
    text = response.text[:10000]  # First 10K chars is plenty

    if _EXPIRED_RE.search(text):
        return FilterResult(eligible=False, reason="expired", platform=platform)

    # 8. Optional: AI classification for ambiguous cases
    if use_ai and profile:
        try:
            from agent1.ai import classify_job
            result = classify_job(text, profile)
            if result.get("expired"):
                return FilterResult(eligible=False, reason="expired", platform=platform)
            if not result.get("eligible"):
                reason = result.get("reason", "not_eligible")
                return FilterResult(eligible=False, reason=reason, platform=platform)
        except Exception as e:
            logger.debug("AI classification failed: %s", e)
            # Fall through — let it pass

    # 9. Passed all checks
    return FilterResult(eligible=True, reason="ok", platform=platform)


def prefilter_batch(urls: list[str], use_ai: bool = False,
                    profile: dict | None = None) -> dict[str, FilterResult]:
    """Pre-filter a batch of job URLs.

    Args:
        urls: List of job URLs.
        use_ai: Whether to use AI for ambiguous cases.
        profile: User profile dict.

    Returns:
        Dict mapping URL -> FilterResult.
    """
    results = {}
    for url in urls:
        results[url] = prefilter_job(url, use_ai=use_ai, profile=profile)
    return results
