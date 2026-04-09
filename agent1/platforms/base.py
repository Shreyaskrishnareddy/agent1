"""Base class for ATS platform applicants.

Each platform script inherits from PlatformApplicant and implements
the apply() method with platform-specific form-filling logic.
"""

import logging
from abc import ABC, abstractmethod

from agent1.browser import Browser

logger = logging.getLogger(__name__)


class PlatformApplicant(ABC):
    """Abstract base for platform-specific application logic.

    Subclasses implement apply() with the deterministic form-filling
    steps for their ATS platform. AI is only used for screening questions
    and unknown fields.
    """

    def __init__(
        self,
        browser: Browser,
        profile: dict,
        resume_text: str,
        resume_pdf_path: str,
        job: dict,
    ):
        self.browser = browser
        self.profile = profile
        self.personal = profile.get("personal", {})
        self.work_auth = profile.get("work_authorization", {})
        self.compensation = profile.get("compensation", {})
        self.experience = profile.get("experience", {})
        self.eeo = profile.get("eeo_voluntary", {})
        self.availability = profile.get("availability", {})
        self.resume_text = resume_text
        self.resume_pdf_path = resume_pdf_path
        self.job = job
        self.b = browser  # shorthand

    @abstractmethod
    def apply(self) -> str:
        """Run the full application flow.

        Returns one of:
            "applied" — submitted successfully
            "expired" — job closed
            "captcha" — blocked by CAPTCHA
            "login_issue" — auth failed
            "failed:{reason}" — other failure
        """
        ...

    def _try_fill(self, selector: str, value: str) -> bool:
        """Try to fill a field, return True if successful."""
        try:
            if self.b.query(selector):
                self.b.fill(selector, value)
                return True
        except Exception:
            pass
        return False

    def _try_click(self, selector: str) -> bool:
        """Try to click an element, return True if successful."""
        try:
            if self.b.query(selector):
                self.b.click(selector)
                return True
        except Exception:
            pass
        return False

    def _try_select(self, selector: str, value: str) -> bool:
        """Try to select a dropdown option, return True if successful."""
        try:
            if self.b.query(selector):
                self.b.select(selector, value)
                return True
        except Exception:
            pass
        return False

    def _try_check(self, selector: str) -> bool:
        """Try to check a checkbox, return True if successful."""
        try:
            if self.b.query(selector):
                self.b.check(selector)
                return True
        except Exception:
            pass
        return False

    def _try_upload(self, selector: str, path: str) -> bool:
        """Try to upload a file, return True if successful."""
        try:
            if self.b.query(selector):
                self.b.upload_file(selector, path)
                return True
        except Exception:
            pass
        return False

    def _page_has_text(self, *phrases: str) -> bool:
        """Check if the page contains any of the given phrases (case-insensitive)."""
        try:
            text = self.b.page_text().lower()
            return any(p.lower() in text for p in phrases)
        except Exception:
            return False

    def _answer_screening(self, question: str, options: list[str] | None = None) -> str:
        """Use Gemma 4 to answer a screening question."""
        from agent1.ai import answer_question
        job_context = f"{self.job.get('title', '')} at {self.job.get('company_name', self.job.get('site', ''))}"
        return answer_question(
            question=question,
            options=options,
            profile=self.profile,
            resume_text=self.resume_text,
            job_context=job_context,
        )

    @property
    def first_name(self) -> str:
        full = self.personal.get("full_name", "")
        return full.split()[0] if full else ""

    @property
    def last_name(self) -> str:
        full = self.personal.get("full_name", "")
        parts = full.split()
        return parts[-1] if len(parts) > 1 else ""

    @property
    def email(self) -> str:
        return self.personal.get("email", "")

    @property
    def phone(self) -> str:
        return self.personal.get("phone", "")

    @property
    def phone_digits(self) -> str:
        return "".join(c for c in self.phone if c.isdigit())

    @property
    def city(self) -> str:
        return self.personal.get("city", "")

    @property
    def linkedin(self) -> str:
        return self.personal.get("linkedin_url", "")

    @property
    def github(self) -> str:
        return self.personal.get("github_url", "")

    @property
    def website(self) -> str:
        return self.personal.get("website_url", "") or self.personal.get("portfolio_url", "")
