"""ATS platform detection from job URLs.

Given a URL, identifies which Application Tracking System hosts it.
This determines whether to use a deterministic platform script or
the AI-assisted fallback.
"""

import re

# URL patterns for known ATS platforms.
# Order matters — more specific patterns should come first.
PLATFORM_PATTERNS: dict[str, list[str]] = {
    "greenhouse": [
        r"boards\.greenhouse\.io",
        r"job-boards\.greenhouse\.io",
    ],
    "lever": [
        r"jobs\.lever\.co",
        r"\.lever\.co",
    ],
    "workday": [
        r"\.wd\d+\.myworkdayjobs",
        r"\.myworkdaysite\.com",
        r"\.myworkdayjobs\.com",
    ],
    "ashby": [
        r"jobs\.ashbyhq\.com",
    ],
    "icims": [
        r"\.icims\.com",
    ],
    "bamboohr": [
        r"\.bamboohr\.com/careers",
        r"\.bamboohr\.com/hiring",
    ],
    "smartrecruiters": [
        r"jobs\.smartrecruiters\.com",
    ],
    "rippling": [
        r"ats\.rippling\.com",
    ],
    "jobvite": [
        r"\.jobvite\.com",
    ],
    "taleo": [
        r"\.taleo\.net",
    ],
    # Platforms we skip — they redirect to login or aren't direct applications
    "linkedin": [
        r"linkedin\.com/jobs",
    ],
}

# Compiled patterns cache
_compiled: dict[str, list[re.Pattern]] = {}


def _get_compiled() -> dict[str, list[re.Pattern]]:
    """Compile and cache regex patterns."""
    if not _compiled:
        for platform, patterns in PLATFORM_PATTERNS.items():
            _compiled[platform] = [re.compile(p, re.IGNORECASE) for p in patterns]
    return _compiled


def detect_platform(url: str) -> str:
    """Detect the ATS platform from a job URL.

    Args:
        url: The job listing or application URL.

    Returns:
        Platform name (e.g. "greenhouse", "lever") or "unknown".
    """
    compiled = _get_compiled()

    for platform, patterns in compiled.items():
        for pattern in patterns:
            if pattern.search(url):
                return platform

    return "unknown"


def is_skippable_platform(platform: str) -> bool:
    """Check if a platform should be skipped (e.g. LinkedIn requires login)."""
    return platform in ("linkedin",)


def get_supported_platforms() -> list[str]:
    """Return list of platforms that have deterministic scripts."""
    # Updated as platform scripts are added
    return ["greenhouse"]  # Add more as Phase 7 progresses
