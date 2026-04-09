"""Gemma 4 API wrapper via Google AI Studio.

Provides targeted AI functions for job application automation:
- ask() / ask_json() — general text/JSON generation
- classify_job() — pre-filter job eligibility
- answer_question() — screening question answers
- describe_page() — multimodal page analysis
"""

import json
import logging
import os
import re

from agent1 import config

logger = logging.getLogger(__name__)

_client = None


def get_client():
    """Get or create the Google GenAI client singleton."""
    global _client
    if _client is not None:
        return _client

    config.load_env()
    api_key = os.environ.get("GOOGLE_AI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "GOOGLE_AI_API_KEY not set. "
            "Get a free key at https://aistudio.google.com/apikey "
            "and add it to ~/.agent1/.env"
        )

    from google import genai
    _client = genai.Client(api_key=api_key)
    return _client


def get_model() -> str:
    """Return the configured Gemma model name."""
    return config.DEFAULTS.get("gemma_model", "gemma-4-26b-a4b-it")


def ask(prompt: str, system: str = "", temperature: float = 0.2) -> str:
    """Simple text-in text-out call to Gemma 4.

    Args:
        prompt: The user prompt.
        system: Optional system instruction.
        temperature: Sampling temperature (lower = more deterministic).

    Returns:
        Response text, stripped.
    """
    client = get_client()
    model = get_model()

    kwargs = {"model": model, "contents": prompt}

    if system:
        from google.genai import types
        kwargs["config"] = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
        )
    else:
        from google.genai import types
        kwargs["config"] = types.GenerateContentConfig(
            temperature=temperature,
        )

    response = client.models.generate_content(**kwargs)
    return response.text.strip()


def ask_json(prompt: str, system: str = "") -> dict | list:
    """Call Gemma 4 and parse the response as JSON.

    Handles responses that wrap JSON in markdown code blocks.

    Args:
        prompt: The user prompt (should ask for JSON output).
        system: Optional system instruction.

    Returns:
        Parsed JSON as dict or list.

    Raises:
        ValueError: If response cannot be parsed as JSON.
    """
    text = ask(prompt, system=system)

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { or [ to end of string
    for i, ch in enumerate(text):
        if ch in ('{', '['):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not parse JSON from response: {text[:200]}")


def classify_job(page_text: str, profile: dict) -> dict:
    """Classify a job listing for eligibility.

    Args:
        page_text: Text content from the job page (first ~3000 chars).
        profile: User profile dict.

    Returns:
        Dict with keys: eligible (bool), reason (str), expired (bool).
    """
    city = profile.get("personal", {}).get("city", "")
    country = profile.get("personal", {}).get("country", "US")

    prompt = f"""Analyze this job listing and answer in JSON format.

Job page text (first 3000 chars):
{page_text[:3000]}

Applicant location: {city}, {country}

Return JSON with exactly these keys:
- "eligible": true/false (is this job in the United States or remote?)
- "expired": true/false (does the page say the job is closed, filled, or no longer accepting applications?)
- "reason": brief explanation (e.g. "remote job", "located in UK", "position filled")

Return ONLY the JSON object, no other text."""

    try:
        result = ask_json(prompt)
        return {
            "eligible": bool(result.get("eligible", True)),
            "expired": bool(result.get("expired", False)),
            "reason": str(result.get("reason", "unknown")),
        }
    except (ValueError, KeyError) as e:
        logger.warning("classify_job parse error: %s", e)
        return {"eligible": True, "expired": False, "reason": "classification_failed"}


def answer_question(
    question: str,
    options: list[str] | None,
    profile: dict,
    resume_text: str,
    job_context: str,
) -> str:
    """Answer a screening question using profile and resume context.

    Args:
        question: The screening question text.
        options: Available options (for select/radio fields), or None for free text.
        profile: User profile dict.
        resume_text: Plain-text resume content.
        job_context: Job title, company, description snippet.

    Returns:
        The answer string (or selected option).
    """
    personal = profile.get("personal", {})
    work_auth = profile.get("work_authorization", {})
    exp = profile.get("experience", {})

    profile_summary = (
        f"Name: {personal.get('full_name', '')}\n"
        f"Location: {personal.get('city', '')}, {personal.get('country', '')}\n"
        f"Work authorized: {work_auth.get('legally_authorized_to_work', '')}\n"
        f"Needs sponsorship: {work_auth.get('require_sponsorship', '')}\n"
        f"Years experience: {exp.get('years_of_experience_total', '')}\n"
        f"Target role: {exp.get('target_role', '')}\n"
    )

    options_text = ""
    if options:
        options_text = f"\nAvailable options (pick the best one EXACTLY as written):\n"
        for opt in options:
            options_text += f"  - {opt}\n"

    prompt = f"""You are answering a screening question for a job application.

Job: {job_context}

Applicant profile:
{profile_summary}

Resume (key points):
{resume_text[:2000]}

Question: {question}
{options_text}
Rules:
- Hard facts (location, work auth, citizenship, criminal history): answer truthfully from the profile.
- Skills/tools questions: answer YES confidently if the skill is in the same domain as the resume.
- Open-ended questions ("Why this role?", "Tell us about yourself"): Write 2-3 specific sentences. Reference the job and a real achievement from the resume. No generic fluff.
- EEO/demographics: "Decline to self-identify" or "Prefer not to say".
- Yes/No questions about willingness (relocate, travel, background check): answer Yes.
{"- Return EXACTLY one of the provided options, nothing else." if options else "- Return just the answer text, nothing else."}"""

    return ask(prompt)


def describe_page(screenshot_bytes: bytes, instruction: str) -> str:
    """Analyze a page screenshot using Gemma 4 multimodal.

    Args:
        screenshot_bytes: PNG screenshot as bytes.
        instruction: What to look for (e.g. "List all form fields").

    Returns:
        Text description/analysis of the page.
    """
    import base64
    client = get_client()
    model = get_model()
    from google.genai import types

    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                parts=[
                    types.Part(text=instruction),
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png",
                            data=screenshot_bytes,
                        )
                    ),
                ]
            )
        ],
        config=types.GenerateContentConfig(temperature=0.2),
    )
    return response.text.strip()
