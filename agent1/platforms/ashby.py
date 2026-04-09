"""Ashby ATS platform script.

Ashby forms at jobs.ashbyhq.com/{company}/{id}/application have:
- System fields: _systemfield_name, _systemfield_email
- Custom fields with UUID names
- Resume/cover letter file uploads
- Radio buttons for EEO (gender, race, veteran, disability)
- Checkboxes for work auth questions
- Location autocomplete
- reCAPTCHA at the bottom
"""

import logging
import time

from agent1.platforms.base import PlatformApplicant

logger = logging.getLogger(__name__)


class AshbyApplicant(PlatformApplicant):
    """Deterministic form filler for Ashby ATS."""

    def apply(self) -> str:
        b = self.b

        # 1. Navigate
        url = self.job["url"]
        if "/application" not in url:
            url = url.rstrip("/") + "/application"
        b.goto(url)
        time.sleep(3)

        # 2. Check expired
        if self._page_has_text(
            "no longer accepting",
            "this job is no longer available",
            "position has been filled",
            "page not found",
        ):
            return "expired"

        # 3. Check form is present
        if not b.query('input[name="_systemfield_name"]') and not b.query('input[name*="name"]'):
            if self._page_has_text("no longer"):
                return "expired"
            return "failed:no_application_form"

        # 4. Fill standard fields
        self._fill_standard_fields()

        # 5. Upload resume
        self._upload_resume()

        # 6. Handle custom questions
        self._handle_custom_questions()

        # 7. Handle EEO
        self._handle_eeo()

        # 8. Consent checkbox
        self._try_check('input[name="I agree"]')

        # 9. Submit
        return self._submit()

    def _fill_standard_fields(self) -> None:
        b = self.b

        # Name
        self._try_fill('input[name="_systemfield_name"]', self.personal.get("full_name", ""))

        # Email
        self._try_fill('input[name="_systemfield_email"]', self.email)

        # Phone — Ashby uses UUID-named phone fields
        phone_fields = b.query_all('input[type="tel"]')
        for pf in phone_fields:
            try:
                pf.fill(self.phone)
                break
            except Exception:
                continue

        # Location — autocomplete
        loc_input = b.query('input[placeholder*="Start typing"]')
        if loc_input:
            try:
                loc_input.fill(self.city)
                time.sleep(1)
                # Click first suggestion
                suggestion = b.query('[class*="option"], [role="option"], li[class*="result"]')
                if suggestion:
                    suggestion.click()
                    time.sleep(0.5)
            except Exception:
                pass

    def _upload_resume(self) -> None:
        b = self.b

        # Ashby has file inputs for Resume and Cover Letter
        # Find them by looking at the parent label
        file_inputs = b.query_all('input[type="file"]')
        for fi in file_inputs:
            try:
                # Check parent for "Resume" text
                parent_text = fi.evaluate(
                    'el => el.closest("[class*=field]")?.querySelector("label")?.textContent || ""'
                )
                if "resume" in parent_text.lower():
                    fi.set_input_files(self.resume_pdf_path)
                    logger.info("Resume uploaded to Ashby")
                    time.sleep(1)
                    return
            except Exception:
                continue

        # Fallback: first file input
        if file_inputs:
            try:
                file_inputs[0].set_input_files(self.resume_pdf_path)
                time.sleep(1)
            except Exception:
                logger.warning("Could not upload resume to Ashby")

    def _handle_custom_questions(self) -> None:
        """Handle Ashby's custom questions (text, textarea, radio, checkbox, select)."""
        b = self.b

        # Get all form field entries
        field_entries = b.query_all(
            '.ashby-application-form-field-entry, '
            '[class*="FormFieldEntry"], '
            '[class*="application-form-field"]'
        )

        for entry in field_entries:
            try:
                label_el = entry.query_selector('label')
                if not label_el:
                    continue
                label = label_el.text_content().strip()
                if not label:
                    continue

                # Skip already-handled fields
                label_lower = label.lower()
                if any(kw in label_lower for kw in [
                    "name", "email", "resume", "cover letter",
                    "gender", "race", "veteran", "disability",
                    "location",
                ]):
                    continue

                # Text input
                text_input = entry.query_selector('input[type="text"]')
                if text_input:
                    name = text_input.get_attribute("name") or ""
                    if name.startswith("_systemfield"):
                        continue
                    # LinkedIn field
                    if "linkedin" in label_lower:
                        text_input.fill(self.linkedin)
                        continue
                    answer = self._answer_screening(label)
                    text_input.fill(answer)
                    continue

                # Textarea
                textarea = entry.query_selector('textarea')
                if textarea:
                    name = textarea.get_attribute("name") or ""
                    if "recaptcha" in name:
                        continue
                    answer = self._answer_screening(label)
                    textarea.fill(answer)
                    continue

                # Radio buttons
                radios = entry.query_selector_all('input[type="radio"]')
                if radios:
                    self._handle_ashby_radio(label, radios)
                    continue

                # Checkboxes
                checkbox = entry.query_selector('input[type="checkbox"]')
                if checkbox:
                    # Work auth / consent checkboxes
                    if any(kw in label_lower for kw in [
                        "authorized", "lawfully", "in person", "onsite",
                    ]):
                        if self.work_auth.get("legally_authorized_to_work"):
                            try:
                                checkbox.check()
                            except Exception:
                                pass
                    elif "sponsorship" in label_lower or "require" in label_lower:
                        if not self.work_auth.get("require_sponsorship"):
                            try:
                                checkbox.check()
                            except Exception:
                                pass
                    continue

                # Select dropdown
                select = entry.query_selector('select')
                if select:
                    options = [o.text_content().strip() for o in
                               select.query_selector_all('option')
                               if o.get_attribute("value")]
                    if options:
                        answer = self._profile_answer_for_select(label, options)
                        if not answer:
                            answer = self._answer_screening(label, options)
                        for o in select.query_selector_all('option'):
                            if o.text_content().strip() == answer:
                                select.select_option(o.get_attribute("value") or "")
                                break

            except Exception as e:
                logger.debug("Error handling Ashby field '%s': %s",
                             label if 'label' in dir() else 'unknown', e)
                continue

    def _handle_ashby_radio(self, question: str, radios) -> None:
        """Handle radio button groups in Ashby forms."""
        options = []
        for rb in radios:
            label = rb.evaluate(
                'el => el.closest("label")?.textContent?.trim() '
                '|| el.parentElement?.textContent?.trim() || ""'
            )
            if label:
                options.append((label, rb))

        if not options:
            return

        opt_texts = [o[0] for o in options]
        answer = self._profile_answer_for_select(question, opt_texts)
        if not answer:
            answer = self._answer_screening(question, opt_texts)

        answer_lower = answer.lower()
        for label, rb in options:
            if answer_lower in label.lower() or label.lower().startswith(answer_lower):
                rb.click()
                return

    def _handle_eeo(self) -> None:
        """Handle EEO radio buttons — Ashby uses _systemfield_eeoc_* naming."""
        b = self.b

        # Gender — select "Decline"
        gender_radios = b.query_all('input[name*="eeoc_gender"]')
        for rb in gender_radios:
            label = rb.evaluate('el => el.closest("label")?.textContent?.trim() || ""')
            if "decline" in label.lower() or "prefer not" in label.lower():
                rb.click()
                break

        # Race — select "Decline"
        race_radios = b.query_all('input[name*="eeoc_race"]')
        for rb in race_radios:
            label = rb.evaluate('el => el.closest("label")?.textContent?.trim() || ""')
            if "decline" in label.lower() or "prefer not" in label.lower():
                rb.click()
                break

        # Veteran — select "not a protected veteran" or "decline"
        vet_radios = b.query_all('input[name*="eeoc_veteran"]')
        for rb in vet_radios:
            label = rb.evaluate('el => el.closest("label")?.textContent?.trim() || ""')
            if "not" in label.lower() or "decline" in label.lower():
                rb.click()
                break

        # Disability — select "do not wish" or "decline"
        dis_radios = b.query_all('input[name*="eeoc_disability"]')
        for rb in dis_radios:
            label = rb.evaluate('el => el.closest("label")?.textContent?.trim() || ""')
            if "do not wish" in label.lower() or "decline" in label.lower():
                rb.click()
                break

    def _submit(self) -> str:
        b = self.b

        submitted = (
            self._try_click('button[type="submit"]')
            or self._try_click('button:has-text("Submit")')
            or self._try_click('button:has-text("Submit application")')
        )

        if not submitted:
            return "failed:no_submit_button"

        time.sleep(3)

        # Check for reCAPTCHA
        has_captcha = b.query('iframe[src*="recaptcha"], .g-recaptcha')
        if has_captcha:
            return "captcha"

        if self._page_has_text(
            "thank you",
            "application received",
            "submitted",
            "we have received",
        ):
            return "applied"

        if self._page_has_text("required", "error", "please fill"):
            return "failed:validation_error"

        time.sleep(2)
        if self._page_has_text("thank you", "submitted"):
            return "applied"

        return "applied"
