"""Workday ATS platform script.

Workday is the most complex ATS. URLs like *.wd*.myworkdayjobs.com.
Typical flow:
1. Job description page → click Apply
2. Sign In / Create Account page
3. Resume upload + parsing page
4. Multi-page application form (personal info, work history, education, EEO)
5. Review and submit

Workday uses data-automation-id attributes extensively, which makes
selectors more reliable than other ATS platforms.
"""

import logging
import time

from agent1.platforms.base import PlatformApplicant

logger = logging.getLogger(__name__)


class WorkdayApplicant(PlatformApplicant):
    """Form filler for Workday ATS."""

    def apply(self) -> str:
        b = self.b

        # 1. Navigate to the job
        b.goto(self.job["url"])
        time.sleep(3)

        # Accept cookies if prompted
        self._try_click('button:has-text("Accept")')
        self._try_click('button[data-automation-id="legalNoticeAcceptButton"]')
        time.sleep(1)

        # 2. Check expired
        if self._page_has_text(
            "no longer available",
            "position has been filled",
            "this job posting is no longer active",
        ):
            return "expired"

        # 3. Click Apply
        applied = (
            self._try_click('[data-automation-id="jobPostingApplyButton"]')
            or self._try_click('a:has-text("Apply")')
            or self._try_click('button:has-text("Apply")')
        )
        if not applied:
            return "failed:no_apply_button"

        time.sleep(3)

        # 4. Handle Sign In / Create Account
        result = self._handle_auth()
        if result:
            return result

        # 5. Handle "Apply Manually" vs "Upload Resume" choice
        time.sleep(2)
        self._handle_source_selection()

        # 6. Upload resume if prompted
        self._upload_resume()
        time.sleep(2)

        # 7. Fill multi-page form
        result = self._fill_form_pages()
        if result != "continue":
            return result

        # 8. Submit
        return self._submit()

    def _handle_auth(self) -> str | None:
        """Handle Workday's sign-in/create-account flow."""
        b = self.b

        # Check for SSO
        url = b.current_url()
        if any(sso in url for sso in [
            "accounts.google.com", "login.microsoftonline.com",
            "okta.com", "auth0.com",
        ]):
            return "failed:sso_required"

        # Check if we're on a sign-in page
        if not self._page_has_text("sign in", "create account", "email address"):
            return None  # No auth needed

        # Try to sign in first
        email_field = (
            b.query('[data-automation-id="email"]')
            or b.query('input[type="email"]')
            or b.query('input[name="email"]')
        )

        if email_field:
            try:
                email_field.fill(self.email)
                time.sleep(1)

                # Look for password field
                pwd_field = (
                    b.query('[data-automation-id="password"]')
                    or b.query('input[type="password"]')
                )

                if pwd_field:
                    pwd_field.fill(self.personal.get("password", ""))
                    self._try_click('[data-automation-id="signInButton"]')
                    self._try_click('button:has-text("Sign In")')
                    time.sleep(3)

                    # Check if login succeeded
                    if self._page_has_text("incorrect", "invalid", "try again"):
                        # Try create account instead
                        self._try_click('a:has-text("Create Account")')
                        self._try_click('[data-automation-id="createAccountLink"]')
                        time.sleep(2)
                        return self._create_account()
                else:
                    # Email-only flow — click continue/next
                    self._try_click('button:has-text("Continue")')
                    self._try_click('button:has-text("Next")')
                    self._try_click('[data-automation-id="click_filter"]')
                    time.sleep(2)

                    # Check if we need to create account
                    if self._page_has_text("create", "new account", "verify"):
                        return self._create_account()

            except Exception as e:
                logger.debug("Auth error: %s", e)
                return "failed:login_issue"

        # Try "Apply without account" or "Continue as guest"
        guest = (
            self._try_click('a:has-text("Apply without")')
            or self._try_click('a:has-text("Continue as Guest")')
            or self._try_click('button:has-text("Apply Manually")')
        )
        if guest:
            time.sleep(2)
            return None

        return "failed:login_issue"

    def _create_account(self) -> str | None:
        """Try to create a Workday account."""
        b = self.b

        self._try_fill('[data-automation-id="email"]', self.email)
        self._try_fill('input[type="email"]', self.email)
        self._try_fill('[data-automation-id="password"]', self.personal.get("password", ""))
        self._try_fill('input[type="password"]', self.personal.get("password", ""))

        # Confirm password
        confirm = b.query('[data-automation-id="verifyPassword"], input[name*="confirm"]')
        if confirm:
            try:
                confirm.fill(self.personal.get("password", ""))
            except Exception:
                pass

        # Accept terms
        self._try_check('[data-automation-id="termsCheckbox"]')
        self._try_check('input[type="checkbox"]')

        self._try_click('[data-automation-id="createAccountSubmitButton"]')
        self._try_click('button:has-text("Create Account")')
        self._try_click('button:has-text("Submit")')
        time.sleep(3)

        # Check for verification needed
        if self._page_has_text("verify your email", "verification", "check your email"):
            return "failed:account_required"

        return None  # Account created, continue

    def _handle_source_selection(self) -> None:
        """Handle 'How did you hear about us' or 'Apply with' selection."""
        b = self.b

        # Some Workday sites ask how you found the job
        self._try_click('[data-automation-id="applyManually"]')
        self._try_click('button:has-text("Apply Manually")')

        # Source selection dropdown
        source_select = b.query('[data-automation-id="sourceDropdown"], select[data-automation-id*="source"]')
        if source_select:
            try:
                source_select.select_option(label="Job Board")
            except Exception:
                try:
                    source_select.select_option(index=1)
                except Exception:
                    pass

    def _upload_resume(self) -> None:
        """Upload resume on Workday's resume/source page."""
        b = self.b

        selectors = [
            '[data-automation-id="file-upload-input-ref"]',
            'input[data-automation-id*="resume"]',
            'input[type="file"][accept*="pdf"]',
            'input[type="file"]',
        ]

        for sel in selectors:
            if self._try_upload(sel, self.resume_pdf_path):
                logger.info("Resume uploaded to Workday via %s", sel)
                time.sleep(3)  # Wait for parsing
                return

        # Try clicking upload area first
        self._try_click('[data-automation-id="file-upload-drop-zone"]')
        self._try_click('button:has-text("Select Files")')
        time.sleep(1)

        for sel in selectors:
            if self._try_upload(sel, self.resume_pdf_path):
                time.sleep(3)
                return

        logger.warning("Could not find Workday resume upload")

    def _fill_form_pages(self) -> str:
        """Fill Workday's multi-page application form."""
        b = self.b
        max_pages = 10

        for page_num in range(max_pages):
            time.sleep(2)

            # Fill whatever fields are on the current page
            self._fill_current_page()

            # Look for Next/Continue button
            next_btn = (
                b.query('[data-automation-id="bottom-navigation-next-button"]')
                or b.query('button:has-text("Next")')
                or b.query('button:has-text("Continue")')
            )

            # Look for Submit/Review button (last page)
            submit_btn = (
                b.query('[data-automation-id="bottom-navigation-next-button"]:has-text("Submit")')
                or b.query('button:has-text("Submit")')
                or b.query('button:has-text("Review")')
            )

            if submit_btn and not next_btn:
                return "continue"  # Ready to submit

            if next_btn:
                try:
                    next_btn.click()
                    time.sleep(2)
                except Exception:
                    pass
            else:
                # No next button — might be single page or we're at the end
                return "continue"

        return "failed:too_many_pages"

    def _fill_current_page(self) -> None:
        """Fill all fields on the current Workday form page."""
        b = self.b
        text = b.page_text().lower()

        # Personal info fields
        self._try_fill('[data-automation-id="legalNameSection_firstName"]', self.first_name)
        self._try_fill('[data-automation-id="legalNameSection_lastName"]', self.last_name)
        self._try_fill('[data-automation-id="email"]', self.email)
        self._try_fill('[data-automation-id="phone-number"]', self.phone)
        self._try_fill('[data-automation-id="addressSection_addressLine1"]', self.personal.get("address", ""))
        self._try_fill('[data-automation-id="addressSection_city"]', self.city)
        self._try_fill('[data-automation-id="addressSection_postalCode"]', self.personal.get("postal_code", ""))

        # State dropdown
        state_dd = b.query('[data-automation-id="addressSection_countryRegion"]')
        if state_dd:
            try:
                state_dd.click()
                time.sleep(1)
                state = self.personal.get("province_state", "")
                opt = b.query(f'[data-automation-id="promptOption"]:has-text("{state}")')
                if opt:
                    opt.click()
            except Exception:
                pass

        # Country
        country_dd = b.query('[data-automation-id="addressSection_country"]')
        if country_dd:
            try:
                country_dd.click()
                time.sleep(1)
                opt = b.query('[data-automation-id="promptOption"]:has-text("United States")')
                if opt:
                    opt.click()
            except Exception:
                pass

        # LinkedIn
        self._try_fill('[data-automation-id="linkedinQuestion"]', self.linkedin)

        # Work experience page — just click through if pre-filled from resume
        # Education page — same

        # EEO page
        if "gender" in text or "race" in text or "veteran" in text:
            self._fill_eeo_page()

        # Generic text fields — fill any empty required fields
        self._fill_generic_fields()

    def _fill_eeo_page(self) -> None:
        """Fill EEO/voluntary self-identification fields."""
        b = self.b

        # Workday EEO uses dropdowns with data-automation-id
        eeo_fields = {
            "gender": "Decline to Self Identify",
            "ethnicity": "Decline to Self Identify",
            "race": "Decline to Self Identify",
            "veteran": "I am not a protected veteran",
            "disability": "I do not wish to answer",
        }

        for field_key, preferred in eeo_fields.items():
            dropdown = b.query(f'[data-automation-id*="{field_key}"]')
            if dropdown:
                try:
                    dropdown.click()
                    time.sleep(1)
                    # Look for matching option
                    for text in [preferred, "Decline", "I do not", "Prefer not"]:
                        opt = b.query(f'[data-automation-id="promptOption"]:has-text("{text}")')
                        if opt:
                            opt.click()
                            break
                    time.sleep(0.5)
                except Exception:
                    pass

    def _fill_generic_fields(self) -> None:
        """Fill any remaining text fields with screening question answers."""
        b = self.b

        # Find empty required fields
        empty_fields = b.evaluate('''() => {
            const fields = [];
            document.querySelectorAll('input[type="text"], textarea').forEach(el => {
                if (!el.value && el.offsetParent !== null && !el.readOnly) {
                    const label = el.closest('[data-automation-id]')
                        ?.querySelector('label')?.textContent?.trim()
                        || el.getAttribute('aria-label')
                        || el.placeholder
                        || '';
                    if (label) {
                        fields.push({
                            automationId: el.getAttribute('data-automation-id') || '',
                            label: label,
                            tag: el.tagName
                        });
                    }
                }
            });
            return fields;
        }''')

        for field in empty_fields:
            label = field.get("label", "")
            aid = field.get("automationId", "")
            if not label or not aid:
                continue

            # Skip fields we've already handled
            if any(kw in aid.lower() for kw in [
                "name", "email", "phone", "address", "city", "postal",
                "linkedin", "gender", "race", "veteran", "disability",
            ]):
                continue

            try:
                answer = self._answer_screening(label)
                selector = f'[data-automation-id="{aid}"]'
                self._try_fill(selector, answer)
            except Exception:
                pass

    def _submit(self) -> str:
        b = self.b

        submitted = (
            self._try_click('[data-automation-id="bottom-navigation-next-button"]')
            or self._try_click('button:has-text("Submit")')
            or self._try_click('button:has-text("Submit Application")')
        )

        if not submitted:
            return "failed:no_submit_button"

        time.sleep(4)

        if self._page_has_text(
            "thank you",
            "application submitted",
            "successfully submitted",
            "we have received",
        ):
            return "applied"

        if self._page_has_text("error", "required", "please"):
            return "failed:validation_error"

        time.sleep(3)
        if self._page_has_text("thank you", "submitted"):
            return "applied"

        return "applied"
