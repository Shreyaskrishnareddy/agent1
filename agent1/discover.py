"""Job discovery: fetch and parse job listings from GitHub repositories.

Fetches the README.md from a GitHub repo containing a markdown table of
job listings, parses it, and returns structured job dicts ready for
store_jobs().
"""

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

logger = logging.getLogger(__name__)

DEFAULT_REPO = "jobright-ai/2026-Software-Engineer-New-Grad"
SPEEDYAPPLY_REPO = "speedyapply/2026-AI-College-Jobs"
SPEEDYAPPLY_PATH = "NEW_GRAD_USA.md"

_RAW_URL_TEMPLATES = [
    "https://raw.githubusercontent.com/{repo}/refs/heads/main/{path}",
    "https://raw.githubusercontent.com/{repo}/main/{path}",
]
_API_URL_TEMPLATE = "https://api.github.com/repos/{repo}/contents/{path}"

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HREF_RE = re.compile(r'<a\s+href="([^"]*)"', re.IGNORECASE)

# US state abbreviations for location filtering
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}


def fetch_file(repo: str = DEFAULT_REPO, path: str = "README.md") -> str:
    """Fetch a markdown file from a GitHub repository.

    Tries raw.githubusercontent.com first, falls back to GitHub API.

    Raises:
        ConnectionError: If all fetch methods fail.
    """
    for template in _RAW_URL_TEMPLATES:
        url = template.format(repo=repo, path=path)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agent1-cli"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            logger.debug("Raw fetch failed for %s: %s", url, e)
            continue

    api_url = _API_URL_TEMPLATE.format(repo=repo, path=path)
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "agent1-cli",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return base64.b64decode(data["content"]).decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError) as e:
        raise ConnectionError(
            f"Failed to fetch {path} from {repo}. "
            f"Check your internet connection and that the repo exists. ({e})"
        ) from e


fetch_readme = fetch_file


def strip_utm_params(url: str) -> str:
    """Remove UTM tracking parameters from a URL."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in params.items() if not k.startswith("utm_")}
    clean_query = urlencode(filtered, doseq=True)
    return urlunparse(parsed._replace(query=clean_query))


def _extract_link(cell: str) -> tuple[str, str]:
    """Extract (text, url) from a markdown cell like **[Text](url)**."""
    bold = _BOLD_RE.search(cell)
    inner = bold.group(1) if bold else cell.strip()

    link = _LINK_RE.search(inner)
    if link:
        return link.group(1).strip(), link.group(2).strip()

    return inner.strip(), ""


def parse_job_table(readme_text: str, strip_utm: bool = True) -> list[dict]:
    """Parse the markdown table from the README and extract job listings."""
    lines = readme_text.splitlines()
    jobs: list[dict] = []
    in_table = False
    last_company = ""

    for line in lines:
        stripped = line.strip()

        if not in_table:
            if re.search(r"\|\s*Company\s*\|.*Job\s*Title\s*\|", stripped, re.IGNORECASE):
                in_table = True
            continue

        if re.match(r"\|[\s\-:]+\|", stripped):
            continue

        if not stripped.startswith("|"):
            break

        cells = [c.strip() for c in stripped.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        if len(cells) < 4:
            continue

        company_cell = cells[0]
        title_cell = cells[1]
        location = cells[2].strip() if len(cells) > 2 else ""
        work_model = cells[3].strip() if len(cells) > 3 else ""

        if company_cell.strip() in ("↳", "\u21b3"):
            company_name = last_company
        else:
            company_name, _ = _extract_link(company_cell)
            if company_name and company_name not in ("↳", "\u21b3"):
                last_company = company_name

        title, job_url = _extract_link(title_cell)

        if not job_url:
            logger.debug("Skipping row with no job URL: %s", title)
            continue

        if strip_utm:
            job_url = strip_utm_params(job_url)

        jobs.append({
            "url": job_url,
            "title": title,
            "company_name": company_name,
            "location": location,
            "work_model": work_model,
        })

    return jobs


def is_us_location(location: str) -> bool:
    """Check if a location string indicates a US-based or remote job."""
    loc = location.strip()
    if not loc:
        return False

    loc_lower = loc.lower()

    if "united states" in loc_lower or loc_lower == "remote":
        return True

    parts = [p.strip() for p in loc.replace(",", " ").split()]
    for part in parts:
        if part.upper() in _US_STATES:
            return True

    return False


def parse_speedyapply_table(text: str, strip_utm: bool = True) -> list[dict]:
    """Parse job tables from speedyapply repos (HTML <a href> format)."""
    lines = text.splitlines()
    jobs: list[dict] = []
    in_table = False
    posting_col = -1

    for line in lines:
        stripped = line.strip()

        if not in_table:
            if re.search(r"\|\s*Company\s*\|.*Position\s*\|", stripped, re.IGNORECASE):
                in_table = True
                hdr_cells = [c.strip() for c in stripped.split("|")]
                hdr_cells = [c for c in hdr_cells if c != ""]
                posting_col = next(
                    (i for i, c in enumerate(hdr_cells) if c.lower() == "posting"), -1
                )
            continue

        if re.match(r"\|[\s\-:]+\|", stripped):
            continue

        if not stripped.startswith("|"):
            in_table = False
            continue

        cells = [c.strip() for c in stripped.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        if len(cells) < 3:
            continue

        company_cell = cells[0]
        company_name = re.sub(r"<[^>]+>", "", company_cell).strip()

        title = re.sub(r"<[^>]+>", "", cells[1]).strip()
        location = re.sub(r"<[^>]+>", "", cells[2]).strip()

        job_url = ""
        if posting_col >= 0 and posting_col < len(cells):
            href_match = _HREF_RE.search(cells[posting_col])
            if href_match:
                job_url = href_match.group(1)

        if not job_url:
            for cell in cells[3:]:
                href_match = _HREF_RE.search(cell)
                if href_match:
                    job_url = href_match.group(1)
                    break

        if not job_url:
            continue

        if strip_utm:
            job_url = strip_utm_params(job_url)

        jobs.append({
            "url": job_url,
            "title": title,
            "company_name": company_name,
            "location": location,
            "work_model": "",
        })

    return jobs


def discover_jobs(
    repo: str = DEFAULT_REPO,
    strip_utm: bool = True,
) -> list[dict]:
    """Fetch and parse job listings from the default repo."""
    readme = fetch_file(repo)
    return parse_job_table(readme, strip_utm=strip_utm)


VALID_SOURCES = ("all", "jobright", "speedyapply")


def discover_all_jobs(
    strip_utm: bool = True, source: str = "all",
) -> tuple[list[dict], dict[str, int]]:
    """Fetch and parse job listings from configured sources."""
    all_jobs: list[dict] = []
    counts: dict[str, int] = {}
    errors: list[str] = []

    if source in ("all", "jobright"):
        try:
            text = fetch_file(DEFAULT_REPO)
            jobright_jobs = parse_job_table(text, strip_utm=strip_utm)
            for j in jobright_jobs:
                j["_source"] = "jobright"
            counts["jobright"] = len(jobright_jobs)
            all_jobs.extend(jobright_jobs)
        except ConnectionError as e:
            errors.append(str(e))

    if source in ("all", "speedyapply"):
        try:
            text = fetch_file(SPEEDYAPPLY_REPO, SPEEDYAPPLY_PATH)
            speedy_jobs = parse_speedyapply_table(text, strip_utm=strip_utm)
            for j in speedy_jobs:
                j["_source"] = "speedyapply"
            counts["speedyapply"] = len(speedy_jobs)
            all_jobs.extend(speedy_jobs)
        except ConnectionError as e:
            errors.append(str(e))

    if not all_jobs and errors:
        raise ConnectionError("; ".join(errors))

    seen: set[str] = set()
    unique: list[dict] = []
    for job in all_jobs:
        if job["url"] not in seen:
            seen.add(job["url"])
            unique.append(job)

    return unique, counts
