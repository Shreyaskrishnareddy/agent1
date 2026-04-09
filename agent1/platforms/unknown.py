"""Unknown platform fallback — AI-assisted form filling.

For job sites that don't match any known ATS platform, this script uses
a combination of DOM inspection and Gemma 4 AI to:
1. Find and click the Apply button
2. Identify form fields
3. Map fields to profile data
4. Fill the form and submit

Slower than deterministic scripts (multiple AI calls) but handles any site.
"""

import logging
import time

from agent1.platforms.base import PlatformApplicant

logger = logging.getLogger(__name__)

# Common field patterns — map label keywords to profile fields
_FIELD_MAP = {
    # Name fields
    "first name": "first_name",
    "given name": "first_name",
    "last name": "last_name",
    "family name": "last_name",
    "surname": "last_name",
    "full name": "full_name",
    "name": "full_name",
    # Contact
    "email": "email",
    "e-mail": "email",
    "phone": "phone",
    "telephone": "phone",
    "mobile": "phone",
    # Location
    "city": "city",
    "location": "city",
    "state": "province_state",
    "province": "province_state",
    "zip": "postal_code",
    "postal": "postal_code",
    "address": "address",
    "country": "country",
    # URLs
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "website": "website_url",
}


class UnknownApplicant(PlatformApplicant):
    """AI-assisted form filler for unknown platforms."""

    def apply(self) -> str:
        b = self.b

        # 1. Navigate
        b.goto(self.job["url"])
        time.sleep(3)

        # 2. Check expired
        if self._page_has_text(
            "no longer available",
            "position has been filled",
            "job not found",
            "page not found",
            "404",
        ):
            return "expired"

        # 3. Find and click Apply button
        if not self._find_and_click_apply():
            # Maybe we're already on the form
            if not self._has_form_fields():
                return "failed:no_apply_button"

        time.sleep(3)

        # 4. Check for SSO/login redirect
        url = b.current_url()
        if any(sso in url for sso in [
            "accounts.google.com", "login.microsoftonline.com",
            "okta.com", "auth0.com", "sso.",
        ]):
            return "failed:sso_required"

        # 5. Handle login wall if present
        if self._page_has_text("sign in", "log in", "create account"):
            result = self._handle_login()
            if result:
                return result

        # 6. Fill form fields using DOM inspection
        self._fill_form_fields()

        # 7. Upload resume
        self._upload_resume()

        # 8. Handle remaining questions with AI
        self._handle_remaining_fields()

        # 9. Submit
        return self._submit()

    def _find_and_click_apply(self) -> bool:
        """Find and click an Apply button."""
        selectors = [
            'a:has-text("Apply Now")',
            'button:has-text("Apply Now")',
            'a:has-text("Apply for this job")',
            'button:has-text("Apply for this job")',
            'a:has-text("Apply")',
            'button:has-text("Apply")',
            'input[value*="Apply"]',
            '[class*="apply"] a',
            '[class*="apply"] button',
            '[id*="apply"] a',
            '[id*="apply"] button',
        ]

        for sel in selectors:
            if self._try_click(sel):
                return True
        return False

    def _has_form_fields(self) -> bool:
        """Check if the current page has form input fields."""
        b = self.b
        inputs = b.query_all('input[type="text"], input[type="email"], textarea')
        return len(inputs) >= 2

    def _handle_login(self) -> str | None:
        """Try to handle login/signup on unknown sites."""
        b = self.b

        # Try email/password login
        email_field = (
            b.query('input[type="email"]')
            or b.query('input[name*="email"]')
            or b.query('input[placeholder*="email" i]')
        )
        if not email_field:
            return "failed:login_issue"

        try:
            email_field.fill(self.email)
        except Exception:
            return "failed:login_issue"

        pwd_field = (
            b.query('input[type="password"]')
            or b.query('input[name*="password"]')
        )
        if pwd_field:
            try:
                pwd_field.fill(self.personal.get("password", ""))
            except Exception:
                pass

        # Click sign in / continue
        self._try_click('button:has-text("Sign In")')
        self._try_click('button:has-text("Log In")')
        self._try_click('button:has-text("Continue")')
        self._try_click('button[type="submit"]')
        time.sleep(3)

        # Check if login worked
        if self._page_has_text("incorrect", "invalid", "failed"):
            # Try create account
            self._try_click('a:has-text("Create")')
            self._try_click('a:has-text("Sign Up")')
            self._try_click('a:has-text("Register")')
            time.sleep(2)

            if self._page_has_text("create", "register", "sign up"):
                self._try_fill('input[type="email"], input[name*="email"]', self.email)
                self._try_fill('input[type="password"]', self.personal.get("password", ""))
                # Try confirm password
                confirms = self.b.query_all('input[type="password"]')
                if len(confirms) > 1:
                    try:
                        confirms[1].fill(self.personal.get("password", ""))
                    except Exception:
                        pass
                self._try_fill('input[name*="first"]', self.first_name)
                self._try_fill('input[name*="last"]', self.last_name)
                self._try_click('button[type="submit"]')
                self._try_click('button:has-text("Create")')
                time.sleep(3)

        # If still on login page, give up
        if self._page_has_text("sign in", "log in") and not self._has_form_fields():
            return "failed:login_issue"

        return None

    def _fill_form_fields(self) -> None:
        """Find form fields and fill them by matching labels to profile data."""
        b = self.b

        # Get all visible form fields with their labels
        fields = b.evaluate('''() => {
            const results = [];
            const inputs = document.querySelectorAll(
                'input[type="text"], input[type="email"], input[type="tel"], '
                + 'input[type="url"], textarea, select'
            );
            for (const el of inputs) {
                if (el.offsetParent === null || el.readOnly || el.disabled) continue;
                if (el.value && el.value.trim()) continue;  // Skip pre-filled

                // Find label
                let label = '';
                const labelEl = el.closest('.field, .form-group, .form-field, [class*=field]')
                    ?.querySelector('label');
                if (labelEl) label = labelEl.textContent.trim();
                if (!label && el.id) {
                    const forLabel = document.querySelector(`label[for="${el.id}"]`);
                    if (forLabel) label = forLabel.textContent.trim();
                }
                if (!label) label = el.placeholder || el.name || el.id || '';

                if (!label) continue;

                // Generate a unique selector
                let selector = '';
                if (el.id) selector = '#' + CSS.escape(el.id);
                else if (el.name) selector = `[name="${el.name}"]`;
                else selector = '';

                results.push({
                    label: label,
                    selector: selector,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                });
            }
            return results;
        }''')

        profile_values = {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.personal.get("full_name", ""),
            "email": self.email,
            "phone": self.phone,
            "city": self.city,
            "province_state": self.personal.get("province_state", ""),
            "postal_code": self.personal.get("postal_code", ""),
            "address": self.personal.get("address", ""),
            "country": self.personal.get("country", ""),
            "linkedin_url": self.linkedin,
            "github_url": self.github,
            "portfolio_url": self.personal.get("portfolio_url", ""),
            "website_url": self.website,
        }

        for field in fields:
            label = field.get("label", "").lower()
            selector = field.get("selector", "")
            if not selector:
                continue

            # Try to match label to a profile field
            matched = False
            for keyword, profile_key in _FIELD_MAP.items():
                if keyword in label:
                    value = profile_values.get(profile_key, "")
                    if value:
                        self._try_fill(selector, value)
                        matched = True
                        break

            if not matched and field["type"] == "email":
                self._try_fill(selector, self.email)
            elif not matched and field["type"] == "tel":
                self._try_fill(selector, self.phone)

    def _upload_resume(self) -> None:
        """Find and use file upload for resume."""
        b = self.b

        file_inputs = b.query_all('input[type="file"]')
        for fi in file_inputs:
            try:
                # Check if this is a resume upload
                parent_text = fi.evaluate(
                    'el => (el.closest("[class*=field], .form-group, label")?.textContent || "").toLowerCase()'
                )
                if any(kw in parent_text for kw in ["resume", "cv", "upload"]):
                    fi.set_input_files(self.resume_pdf_path)
                    logger.info("Resume uploaded on unknown platform")
                    time.sleep(2)
                    return
            except Exception:
                continue

        # Fallback: first file input
        if file_inputs:
            try:
                file_inputs[0].set_input_files(self.resume_pdf_path)
                time.sleep(2)
            except Exception:
                pass

    def _handle_remaining_fields(self) -> None:
        """Use AI for any unfilled required fields."""
        b = self.b

        # Find empty required fields
        empty_fields = b.evaluate('''() => {
            const results = [];
            const inputs = document.querySelectorAll(
                'input[type="text"], textarea'
            );
            for (const el of inputs) {
                if (el.offsetParent === null || el.readOnly || el.disabled) continue;
                if (el.value && el.value.trim()) continue;

                const required = el.required
                    || el.closest('[class*=required]') !== null
                    || el.getAttribute('aria-required') === 'true';

                let label = '';
                const labelEl = el.closest('.field, .form-group, [class*=field]')
                    ?.querySelector('label');
                if (labelEl) label = labelEl.textContent.trim();
                if (!label && el.id) {
                    const forLabel = document.querySelector(`label[for="${el.id}"]`);
                    if (forLabel) label = forLabel.textContent.trim();
                }
                if (!label) label = el.placeholder || '';

                if (label && (required || el.tagName === 'TEXTAREA')) {
                    let selector = '';
                    if (el.id) selector = '#' + CSS.escape(el.id);
                    else if (el.name) selector = `[name="${el.name}"]`;
                    if (selector) {
                        results.push({ label, selector });
                    }
                }
            }
            return results;
        }''')

        for field in empty_fields:
            label = field.get("label", "")
            selector = field.get("selector", "")
            if not label or not selector:
                continue

            try:
                answer = self._answer_screening(label)
                self._try_fill(selector, answer)
            except Exception as e:
                logger.debug("AI answer failed for '%s': %s", label, e)

        # Handle select dropdowns
        selects = b.query_all('select')
        for sel in selects:
            try:
                # Skip if already has a value
                current = sel.evaluate('el => el.value')
                if current:
                    continue

                label = sel.evaluate(
                    'el => el.closest("[class*=field]")?.querySelector("label")?.textContent?.trim() || ""'
                )
                if not label:
                    continue

                options = [o.text_content().strip() for o in sel.query_selector_all('option')
                           if o.get_attribute('value')]
                if not options:
                    continue

                answer = self._profile_answer_for_select(label, options)
                if not answer:
                    answer = self._answer_screening(label, options)

                for o in sel.query_selector_all('option'):
                    if o.text_content().strip() == answer:
                        sel.select_option(o.get_attribute('value') or '')
                        break
            except Exception:
                continue

        # Handle checkboxes (consent, terms, etc.)
        checkboxes = b.query_all('input[type="checkbox"]:not(:checked)')
        for cb in checkboxes:
            try:
                label = cb.evaluate(
                    'el => el.closest("label")?.textContent?.trim() '
                    '|| el.parentElement?.textContent?.trim() || ""'
                ).lower()
                if any(kw in label for kw in [
                    "agree", "consent", "terms", "acknowledge",
                    "authorized", "confirm", "accept",
                ]):
                    cb.check()
            except Exception:
                continue

    def _submit(self) -> str:
        b = self.b

        submitted = (
            self._try_click('button[type="submit"]')
            or self._try_click('input[type="submit"]')
            or self._try_click('button:has-text("Submit")')
            or self._try_click('button:has-text("Submit Application")')
            or self._try_click('button:has-text("Apply")')
            or self._try_click('button:has-text("Send")')
        )

        if not submitted:
            return "failed:no_submit_button"

        time.sleep(4)

        if self._page_has_text(
            "thank you",
            "application received",
            "submitted",
            "we have received",
            "confirmation",
        ):
            return "applied"

        if self._page_has_text("captcha", "verify you're human"):
            return "captcha"

        if self._page_has_text("error", "required", "please fill"):
            return "failed:validation_error"

        time.sleep(3)
        if self._page_has_text("thank you", "submitted", "confirmation"):
            return "applied"

        return "applied"
