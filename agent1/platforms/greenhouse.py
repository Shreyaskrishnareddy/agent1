"""Greenhouse ATS platform script.

Greenhouse forms at boards.greenhouse.io/embed/job_app have a predictable
structure: standard fields (name, email, phone, resume), optional fields
(LinkedIn, website, location), and custom screening questions.

This script fills everything deterministically and only calls AI for
screening questions it can't answer from the profile.
"""

import logging
import time

from agent1.platforms.base import PlatformApplicant

logger = logging.getLogger(__name__)


class GreenhouseApplicant(PlatformApplicant):
    """Deterministic form filler for Greenhouse ATS."""

    def apply(self) -> str:
        """Fill and submit a Greenhouse application form."""
        b = self.b

        # 1. Navigate to the job URL
        b.goto(self.job["url"])
        time.sleep(2)

        # 2. Check if expired
        if self._page_has_text(
            "no longer available",
            "no longer accepting",
            "this job has expired",
            "job not found",
        ):
            return "expired"

        # 3. Find and click Apply if we're on a job description page (not the form)
        if not b.query('#first_name') and not b.query('input[name="job_application[first_name]"]'):
            # Try clicking Apply button
            applied = (
                self._try_click('a[href*="job_app"]')
                or self._try_click('a:has-text("Apply")')
                or self._try_click('button:has-text("Apply")')
            )
            if applied:
                time.sleep(2)

        # 4. Check again if we're on the form
        if not b.query('#first_name') and not b.query('input[name="job_application[first_name]"]'):
            # Maybe the URL is already the form, or we need to look for an iframe
            if self._page_has_text("no longer available", "this job has expired"):
                return "expired"
            return "failed:no_application_form"

        # 5. Fill standard fields
        self._fill_standard_fields()

        # 6. Upload resume
        self._upload_resume()

        # 7. Fill optional fields (LinkedIn, website, location, etc.)
        self._fill_optional_fields()

        # 8. Handle custom questions
        self._handle_custom_questions()

        # 9. Handle EEO/demographic questions
        self._handle_eeo()

        # 10. Submit
        return self._submit()

    def _fill_standard_fields(self) -> None:
        """Fill name, email, phone — the fields every Greenhouse form has."""
        b = self.b

        # First name
        self._try_fill('#first_name', self.first_name)
        self._try_fill('input[name="job_application[first_name]"]', self.first_name)

        # Last name
        self._try_fill('#last_name', self.last_name)
        self._try_fill('input[name="job_application[last_name]"]', self.last_name)

        # Email
        self._try_fill('#email', self.email)
        self._try_fill('input[name="job_application[email]"]', self.email)

        # Phone — handle country code dropdown first
        phone_country = b.query('select[id*="phone_country"], select[name*="phone_country"]')
        if phone_country:
            try:
                # Select US (+1)
                phone_country.select_option(label="United States (+1)")
            except Exception:
                try:
                    phone_country.select_option(value="us")
                except Exception:
                    pass

        # Phone number (digits only if there's a country code prefix)
        phone_val = self.phone_digits if phone_country else self.phone
        self._try_fill('#phone', phone_val)
        self._try_fill('input[name="job_application[phone]"]', phone_val)

    def _upload_resume(self) -> None:
        """Upload resume PDF. Greenhouse uses a file input near 'Resume'."""
        b = self.b

        # Try common selectors for Greenhouse resume upload
        selectors = [
            'input[type="file"][id*="resume"]',
            'input[type="file"][name*="resume"]',
            'input[type="file"][data-field="resume"]',
            '#resume_file_input',
            # Generic fallback: first file input
            'input[type="file"]',
        ]

        for sel in selectors:
            if self._try_upload(sel, self.resume_pdf_path):
                logger.info("Resume uploaded via %s", sel)
                time.sleep(1)
                return

        logger.warning("Could not find resume upload input")

    def _fill_optional_fields(self) -> None:
        """Fill LinkedIn, GitHub, website, location if present."""
        b = self.b

        # LinkedIn
        for sel in ['input[name*="linkedin"]', 'input[id*="linkedin"]',
                     'input[placeholder*="LinkedIn"]', 'input[autocomplete="linkedin"]']:
            if self._try_fill(sel, self.linkedin):
                break

        # GitHub
        for sel in ['input[name*="github"]', 'input[id*="github"]',
                     'input[placeholder*="GitHub"]']:
            if self._try_fill(sel, self.github):
                break

        # Website/Portfolio
        for sel in ['input[name*="website"]', 'input[name*="portfolio"]',
                     'input[id*="website"]', 'input[placeholder*="website"]']:
            if self._try_fill(sel, self.website):
                break

        # Location/City
        for sel in ['input[name*="location"]', 'input[id*="location"]',
                     '#job_application_location']:
            if self._try_fill(sel, self.city):
                break

    def _handle_custom_questions(self) -> None:
        """Find and answer ALL form fields — selects, radios, text, textarea.

        Uses a comprehensive approach: find every unfilled select, textarea,
        text input, and radio group on the page, match to profile or AI.
        """
        b = self.b

        # 1. Handle ALL select dropdowns on the page
        self._fill_all_selects()

        # 2. Handle remaining text/textarea/radio/checkbox fields
        self._fill_remaining_fields()

    def _fill_all_selects(self) -> None:
        """Fill every unfilled <select> dropdown on the page."""
        b = self.b

        selects_info = b.evaluate('''() => {
            return Array.from(document.querySelectorAll('select')).map((sel, idx) => {
                if (sel.value) return null;  // Already filled
                let label = '';
                const container = sel.closest('.field, [class*=field], [class*=question]');
                if (container) {
                    const lbl = container.querySelector('label');
                    if (lbl) label = lbl.textContent.trim();
                }
                if (!label) label = sel.getAttribute('aria-label') || '';
                const opts = Array.from(sel.options)
                    .filter(o => o.value && o.value !== '')
                    .map(o => ({ value: o.value, text: o.textContent.trim() }));
                return { index: idx, label, options: opts, name: sel.name || '', id: sel.id || '' };
            }).filter(x => x !== null);
        }''')

        for info in selects_info:
            label = info.get("label", "")
            options = info.get("options", [])
            opt_texts = [o["text"] for o in options]

            if not opt_texts:
                continue

            # Try profile-based answer
            answer = self._profile_answer_for_select(label, opt_texts)

            if not answer:
                # Try AI
                try:
                    answer = self._answer_screening(label, opt_texts)
                except Exception:
                    answer = opt_texts[0] if opt_texts else ""

            if not answer:
                continue

            # Find matching option value and select it
            target_value = None
            answer_lower = answer.lower().strip()
            for o in options:
                if o["text"].strip() == answer.strip():
                    target_value = o["value"]
                    break
            if not target_value:
                for o in options:
                    if answer_lower in o["text"].lower():
                        target_value = o["value"]
                        break
            if not target_value and options:
                # Last resort: first non-empty option
                target_value = options[0]["value"]

            if target_value:
                try:
                    sel_selector = ""
                    if info.get("id"):
                        sel_selector = f'#{info["id"]}'
                    elif info.get("name"):
                        sel_selector = f'select[name="{info["name"]}"]'
                    else:
                        sel_selector = f'select >> nth={info["index"]}'

                    b.page.select_option(sel_selector, target_value)
                    logger.debug("Selected '%s' for '%s'", answer, label[:40])
                except Exception as e:
                    logger.debug("Failed to select for '%s': %s", label[:40], e)

    def _fill_remaining_fields(self) -> None:
        """Fill unfilled text inputs, textareas, radios, and checkboxes."""
        b = self.b

        standard_names = {
            'first_name', 'last_name', 'email', 'phone',
            'job_application[first_name]', 'job_application[last_name]',
            'job_application[email]', 'job_application[phone]',
        }

        # Text inputs and textareas
        fields = b.evaluate('''() => {
            return Array.from(document.querySelectorAll(
                'input[type="text"]:not([readonly]), textarea'
            )).map(el => {
                if (el.value && el.value.trim()) return null;
                if (el.offsetParent === null) return null;
                let label = '';
                const container = el.closest('.field, [class*=field]');
                if (container) {
                    const lbl = container.querySelector('label');
                    if (lbl) label = lbl.textContent.trim();
                }
                return {
                    name: el.name || '',
                    id: el.id || '',
                    tag: el.tagName.toLowerCase(),
                    label: label
                };
            }).filter(x => x !== null);
        }''')

        for f in fields:
            name = f.get("name", "")
            if name in standard_names:
                continue

            label = f.get("label", "")
            if not label:
                continue

            selector = ""
            if f.get("id"):
                selector = f'#{f["id"]}'
            elif name:
                selector = f'[name="{name}"]'
            else:
                continue

            try:
                answer = self._answer_screening(label)
                self._try_fill(selector, answer)
            except Exception:
                pass

        # Checkboxes — check consent/agreement checkboxes
        checkboxes = b.query_all('input[type="checkbox"]:not(:checked)')
        for cb in checkboxes:
            try:
                label = cb.evaluate(
                    'el => el.closest("label, .field, [class*=field]")?.textContent?.trim() || ""'
                ).lower()
                if any(kw in label for kw in ["agree", "consent", "acknowledge", "confirm"]):
                    cb.check()
            except Exception:
                pass

    def _handle_select_question(self, label: str, select_el) -> None:
        """Handle a <select> dropdown question."""
        try:
            # Get all options
            options = select_el.query_selector_all('option')
            option_texts = []
            for opt in options:
                val = opt.get_attribute('value') or ''
                text = opt.text_content().strip()
                if val and text and text.lower() not in ('select', 'choose', '--', ''):
                    option_texts.append(text)

            if not option_texts:
                return

            # Try profile-based answers first
            answer = self._profile_answer_for_select(label, option_texts)
            if not answer:
                answer = self._answer_screening(label, option_texts)

            # Find the matching option and select it
            for opt in options:
                if opt.text_content().strip() == answer:
                    val = opt.get_attribute('value')
                    if val:
                        select_el.select_option(val)
                        return

            # Fuzzy match: select first option that contains the answer
            answer_lower = answer.lower()
            for opt in options:
                if answer_lower in opt.text_content().strip().lower():
                    val = opt.get_attribute('value')
                    if val:
                        select_el.select_option(val)
                        return

        except Exception as e:
            logger.debug("Error handling select question '%s': %s", label, e)

    def _handle_radio_question(self, label: str, radio_buttons) -> None:
        """Handle a radio button group question."""
        try:
            options = []
            for rb in radio_buttons:
                # Get label for this radio
                rb_id = rb.get_attribute('id') or ''
                rb_label = rb.evaluate(
                    'el => el.parentElement?.textContent?.trim() || el.nextSibling?.textContent?.trim() || ""'
                )
                rb_value = rb.get_attribute('value') or ''
                if rb_label or rb_value:
                    options.append(rb_label or rb_value)

            if not options:
                return

            answer = self._profile_answer_for_select(label, options)
            if not answer:
                answer = self._answer_screening(label, options)

            # Click the matching radio button
            answer_lower = answer.lower()
            for rb in radio_buttons:
                rb_label = rb.evaluate(
                    'el => el.parentElement?.textContent?.trim() || el.nextSibling?.textContent?.trim() || ""'
                )
                rb_value = (rb.get_attribute('value') or '').lower()
                if answer_lower in rb_label.lower() or answer_lower == rb_value:
                    rb.click()
                    return

        except Exception as e:
            logger.debug("Error handling radio question '%s': %s", label, e)

    def _profile_answer_for_select(self, label: str, options: list[str]) -> str | None:
        """Try to answer a select/radio question from profile data (no AI needed)."""
        label_lower = label.lower()

        # Work authorization questions
        if any(kw in label_lower for kw in ["authorized to work", "legally authorized", "work authorization"]):
            target = "yes" if self.work_auth.get("legally_authorized_to_work") else "no"
            for opt in options:
                if opt.lower().startswith(target):
                    return opt

        # Sponsorship questions
        if any(kw in label_lower for kw in ["sponsorship", "visa", "require sponsorship"]):
            target = "yes" if self.work_auth.get("require_sponsorship") else "no"
            for opt in options:
                if opt.lower().startswith(target):
                    return opt

        # Gender / EEO
        if any(kw in label_lower for kw in ["gender", "sex"]):
            for opt in options:
                if "decline" in opt.lower() or "prefer not" in opt.lower():
                    return opt

        # Race/Ethnicity
        if any(kw in label_lower for kw in ["race", "ethnicity"]):
            for opt in options:
                if "decline" in opt.lower() or "prefer not" in opt.lower():
                    return opt

        # Veteran
        if "veteran" in label_lower:
            for opt in options:
                if "not" in opt.lower() or "decline" in opt.lower():
                    return opt

        # Disability
        if "disability" in label_lower or "handicap" in label_lower:
            for opt in options:
                if "do not wish" in opt.lower() or "decline" in opt.lower():
                    return opt

        # Education
        if "education" in label_lower or "degree" in label_lower:
            edu = self.experience.get("education_level", "").lower()
            for opt in options:
                if edu and edu in opt.lower():
                    return opt

        # Yes/No defaults for common questions
        if any(kw in label_lower for kw in ["18 years", "background check", "willing to"]):
            for opt in options:
                if opt.lower().startswith("yes"):
                    return opt

        if "felony" in label_lower or "convicted" in label_lower:
            for opt in options:
                if opt.lower().startswith("no"):
                    return opt

        return None

    def _handle_eeo(self) -> None:
        """Handle EEO/voluntary self-identification sections."""
        # These are usually handled by _profile_answer_for_select
        # but some appear as standalone sections
        pass

    def _submit(self) -> str:
        """Click submit and verify success."""
        b = self.b

        # Find submit button
        submitted = (
            self._try_click('#submit_app')
            or self._try_click('button[type="submit"]')
            or self._try_click('input[type="submit"]')
            or self._try_click('button:has-text("Submit")')
            or self._try_click('button:has-text("Submit Application")')
        )

        if not submitted:
            return "failed:no_submit_button"

        time.sleep(3)

        # Check for success
        if self._page_has_text(
            "thank you",
            "application received",
            "application has been submitted",
            "successfully submitted",
            "we have received your application",
        ):
            return "applied"

        # Check for errors
        if self._page_has_text("required field", "please fill", "error"):
            return "failed:validation_error"

        # Check if we're still on the same page (might need to wait more)
        time.sleep(2)
        if self._page_has_text("thank you", "application received"):
            return "applied"

        # Assume success if no errors visible
        return "applied"
