"""Lever ATS platform script.

Lever forms at jobs.lever.co/{company}/{id}/apply have:
- Standard fields: name, email, phone, location, current company
- URL fields: LinkedIn, GitHub, Twitter, Portfolio, Other
- Resume upload via input[name="resume"]
- Custom questions in cards[uuid][fieldN] format (radio, text, textarea)
- hCaptcha at the bottom
"""

import logging
import time

from agent1.platforms.base import PlatformApplicant

logger = logging.getLogger(__name__)


class LeverApplicant(PlatformApplicant):
    """Deterministic form filler for Lever ATS."""

    def apply(self) -> str:
        b = self.b

        # 1. Navigate
        url = self.job["url"]
        if "/apply" not in url:
            url = url.rstrip("/") + "/apply"
        b.goto(url)
        time.sleep(2)

        # 2. Check expired
        if self._page_has_text(
            "no longer accepting",
            "position is no longer available",
            "this posting has been closed",
            "page not found",
        ):
            return "expired"

        # 3. Check we're on the apply form
        if not b.query('input[name="name"]'):
            # Maybe we're on the job description page
            self._try_click('a[href*="/apply"]')
            self._try_click('a:has-text("Apply")')
            time.sleep(2)

        if not b.query('input[name="name"]'):
            if self._page_has_text("no longer accepting"):
                return "expired"
            return "failed:no_application_form"

        # 4. Fill standard fields
        self._fill_standard_fields()

        # 5. Upload resume
        self._upload_resume()

        # 6. Fill URL fields
        self._fill_url_fields()

        # 7. Handle custom questions (cards)
        self._handle_custom_questions()

        # 8. Submit
        return self._submit()

    def _fill_standard_fields(self) -> None:
        b = self.b

        self._try_fill('input[name="name"]', self.personal.get("full_name", ""))
        self._try_fill('input[name="email"]', self.email)
        self._try_fill('input[name="phone"]', self.phone)
        self._try_fill('input[name="org"]', self.experience.get("current_title", ""))

        # Location — Lever has an autocomplete location field
        loc_input = b.query('input[name="location"]')
        if loc_input:
            try:
                loc_input.fill(self.city)
                time.sleep(1)
                # Try to click the first autocomplete suggestion
                suggestion = b.query('.location-search-results li, .location-result, [class*="location"] li')
                if suggestion:
                    suggestion.click()
                else:
                    # No suggestions — just leave what we typed
                    pass
            except Exception:
                pass

    def _upload_resume(self) -> None:
        selectors = [
            'input[name="resume"]',
            '#resume-upload-input',
            'input[type="file"]',
        ]
        for sel in selectors:
            if self._try_upload(sel, self.resume_pdf_path):
                logger.info("Resume uploaded via %s", sel)
                time.sleep(2)
                return
        logger.warning("Could not find Lever resume upload input")

    def _fill_url_fields(self) -> None:
        self._try_fill('input[name="urls[LinkedIn]"]', self.linkedin)
        self._try_fill('input[name="urls[GitHub]"]', self.github)
        self._try_fill('input[name="urls[Portfolio]"]', self.website)
        self._try_fill('input[name="urls[Other]"]',
                       self.personal.get("website_url", ""))

    def _handle_custom_questions(self) -> None:
        """Handle Lever's card-based custom questions.

        Lever uses cards[uuid][fieldN] naming for custom questions.
        Each card is a question with radio buttons, text, or textarea.
        """
        b = self.b

        # Find all question cards
        cards = b.query_all('.application-question.custom-question, .application-additional')

        for card in cards:
            try:
                # Get question text
                label_el = card.query_selector('.application-label, label, .text-heading')
                if not label_el:
                    continue
                question = label_el.text_content().strip()
                if not question:
                    continue

                # Check for radio buttons
                radios = card.query_selector_all('input[type="radio"]')
                if radios:
                    self._handle_lever_radio(question, radios)
                    continue

                # Check for textarea
                textarea = card.query_selector('textarea')
                if textarea:
                    answer = self._answer_screening(question)
                    textarea.fill(answer)
                    continue

                # Check for text input (not hidden)
                text_input = card.query_selector('input[type="text"]:not([type="hidden"])')
                if text_input:
                    name = text_input.get_attribute("name") or ""
                    if "baseTemplate" in name:
                        continue  # Hidden template field
                    answer = self._answer_screening(question)
                    text_input.fill(answer)
                    continue

                # Check for select
                select = card.query_selector('select')
                if select:
                    options = select.query_selector_all('option')
                    opt_texts = [o.text_content().strip() for o in options
                                 if o.get_attribute("value")]
                    if opt_texts:
                        answer = self._profile_answer_for_select(question, opt_texts)
                        if not answer:
                            answer = self._answer_screening(question, opt_texts)
                        for o in options:
                            if o.text_content().strip() == answer:
                                select.select_option(o.get_attribute("value") or "")
                                break

            except Exception as e:
                logger.debug("Error handling Lever card: %s", e)
                continue

    def _handle_lever_radio(self, question: str, radios) -> None:
        """Handle radio button questions in Lever forms."""
        options = []
        for rb in radios:
            label = rb.evaluate(
                'el => el.closest("li")?.textContent?.trim() '
                '|| el.parentElement?.textContent?.trim() || ""'
            )
            if label:
                options.append((label, rb))

        if not options:
            return

        opt_texts = [o[0] for o in options]

        # Try profile-based answer first
        answer = self._profile_answer_for_select(question, opt_texts)
        if not answer:
            answer = self._answer_screening(question, opt_texts)

        # Click matching radio
        answer_lower = answer.lower()
        for label, rb in options:
            if answer_lower in label.lower() or label.lower().startswith(answer_lower):
                rb.click()
                return

        # Fallback: click first option
        if options:
            options[0][1].click()

    def _submit(self) -> str:
        b = self.b

        submitted = (
            self._try_click('button[type="submit"]')
            or self._try_click('button:has-text("Submit application")')
            or self._try_click('button:has-text("Submit")')
        )

        if not submitted:
            return "failed:no_submit_button"

        time.sleep(3)

        # Check for hCaptcha (Lever uses it frequently)
        has_captcha = b.query('.h-captcha, iframe[src*="hcaptcha"]')
        if has_captcha:
            return "captcha"

        if self._page_has_text(
            "thank you",
            "application received",
            "application has been submitted",
            "we have received",
        ):
            return "applied"

        if self._page_has_text("required", "please fill", "error"):
            return "failed:validation_error"

        time.sleep(2)
        if self._page_has_text("thank you", "application"):
            return "applied"

        return "applied"
