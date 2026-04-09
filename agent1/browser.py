"""Playwright Python browser manager.

Provides a Browser class that wraps Playwright's sync API for
direct browser automation. Supports two modes:
  1. CDP mode: Connect to an existing Chrome via remote debugging (default on WSL/Windows)
  2. Bundled mode: Launch Playwright's own Chromium (requires playwright install)
"""

import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent1 import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222


def _find_chrome() -> str | None:
    """Find Chrome/Chromium executable, cross-platform."""
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        # Linux / WSL — check Windows Chrome paths first (WSL interop)
        candidates = []
        for win_path in [
            "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
            "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        ]:
            if Path(win_path).exists():
                candidates.append(Path(win_path))
        # Then native Linux
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    return None


def _launch_chrome(port: int, user_data_dir: str, headless: bool = False) -> subprocess.Popen:
    """Launch Chrome with remote debugging enabled."""
    chrome_exe = _find_chrome()
    if not chrome_exe:
        raise FileNotFoundError(
            "Chrome not found. Install Chrome or set CHROME_PATH env var."
        )

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--disable-popup-blocking",
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP endpoint to be ready
    import urllib.request
    import urllib.error

    url = f"http://localhost:{port}/json/version"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return proc
        except (urllib.error.URLError, OSError):
            time.sleep(1)

    logger.warning("Chrome CDP not ready after 30s on port %d", port)
    return proc


def _kill_process_tree(pid: int) -> None:
    """Kill a process and its children."""
    import signal as _signal
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


class Browser:
    """Manages a browser instance via Playwright.

    On WSL/Windows: launches Chrome with CDP and connects Playwright to it.
    On Linux with playwright install: uses Playwright's bundled Chromium.

    Usage:
        with Browser(headless=False) as b:
            b.goto("https://example.com")
            b.fill('input[name="email"]', "user@example.com")
            b.click('button[type="submit"]')
    """

    def __init__(
        self,
        headless: bool = False,
        worker_id: int = 0,
        user_data_dir: str | None = None,
    ):
        self.headless = headless
        self.worker_id = worker_id
        self.user_data_dir = user_data_dir or str(
            config.BROWSER_PROFILE_DIR / f"worker-{worker_id}"
        )
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._chrome_proc = None
        self._cdp_port = BASE_CDP_PORT + worker_id

    def _cdp_is_ready(self) -> bool:
        """Check if a Chrome CDP endpoint is already listening."""
        import urllib.request
        import urllib.error
        try:
            urllib.request.urlopen(
                f"http://localhost:{self._cdp_port}/json/version", timeout=2
            )
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _connect_cdp(self) -> None:
        """Connect Playwright to Chrome via CDP."""
        viewport = config.DEFAULTS.get("viewport", "1280x900")
        w, h = (int(x) for x in viewport.split("x"))

        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://localhost:{self._cdp_port}"
        )

        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()
        else:
            self._context = self._browser.new_context(
                viewport={"width": w, "height": h}
            )
            self._page = self._context.new_page()

    def launch(self) -> "Browser":
        """Launch a browser.

        Strategy:
        1. If Chrome CDP is already running on the port, connect to it.
        2. Else, try to launch Chrome with CDP and connect.
        3. Else, fall back to Playwright's bundled Chromium.
        """
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        viewport = config.DEFAULTS.get("viewport", "1280x900")
        w, h = (int(x) for x in viewport.split("x"))

        # Strategy 1: Connect to already-running Chrome
        if self._cdp_is_ready():
            self._connect_cdp()
            logger.info(
                "[worker-%d] Connected to existing Chrome (port %d)",
                self.worker_id, self._cdp_port,
            )
            return self

        # Strategy 2: Launch Chrome with CDP
        chrome_path = _find_chrome()
        if chrome_path:
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            self._chrome_proc = _launch_chrome(
                self._cdp_port, self.user_data_dir, self.headless
            )

            if self._cdp_is_ready():
                self._connect_cdp()
                logger.info(
                    "[worker-%d] Launched Chrome and connected via CDP (port %d)",
                    self.worker_id, self._cdp_port,
                )
                return self

            logger.warning("[worker-%d] Chrome launched but CDP not ready, trying bundled...", self.worker_id)

        # Strategy 3: Bundled Playwright Chromium
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            viewport={"width": w, "height": h},
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
                "--deny-permission-prompts",
                "--disable-notifications",
            ],
        )
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()

        logger.info("[worker-%d] Browser launched (bundled Chromium)", self.worker_id)
        return self

    @property
    def page(self):
        """Access the underlying Playwright page object directly."""
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    def goto(self, url: str, wait: str = "domcontentloaded", timeout: int = 30000) -> None:
        """Navigate to a URL."""
        self.page.goto(url, wait_until=wait, timeout=timeout)

    def fill(self, selector: str, value: str, timeout: int = 5000) -> None:
        """Fill a form field by CSS selector."""
        self.page.fill(selector, value, timeout=timeout)

    def click(self, selector: str, timeout: int = 5000) -> None:
        """Click an element by CSS selector."""
        self.page.click(selector, timeout=timeout)

    def select(self, selector: str, value: str, timeout: int = 5000) -> None:
        """Select an option from a <select> dropdown."""
        self.page.select_option(selector, value, timeout=timeout)

    def check(self, selector: str, timeout: int = 5000) -> None:
        """Check a checkbox."""
        self.page.check(selector, timeout=timeout)

    def upload_file(self, selector: str, file_path: str, timeout: int = 5000) -> None:
        """Upload a file to a file input."""
        self.page.set_input_files(selector, file_path, timeout=timeout)

    def screenshot(self, full_page: bool = False) -> bytes:
        """Take a screenshot, returns PNG bytes."""
        return self.page.screenshot(full_page=full_page)

    def screenshot_to_file(self, path: str, full_page: bool = False) -> None:
        """Save a screenshot to a file."""
        self.page.screenshot(path=path, full_page=full_page)

    def page_text(self) -> str:
        """Get the full text content of the page body."""
        return self.page.inner_text("body")

    def page_html(self) -> str:
        """Get the full HTML content of the page."""
        return self.page.content()

    def current_url(self) -> str:
        """Get the current page URL."""
        return self.page.url

    def title(self) -> str:
        """Get the current page title."""
        return self.page.title()

    def evaluate(self, js: str) -> Any:
        """Execute JavaScript in the page context."""
        return self.page.evaluate(js)

    def wait_for(self, selector: str, timeout: int = 10000, state: str = "visible") -> None:
        """Wait for an element to appear."""
        self.page.wait_for_selector(selector, timeout=timeout, state=state)

    def wait_for_navigation(self, timeout: int = 30000) -> None:
        """Wait for a navigation to complete."""
        self.page.wait_for_load_state("domcontentloaded", timeout=timeout)

    def wait_for_url(self, url_pattern: str, timeout: int = 30000) -> None:
        """Wait for the URL to match a pattern."""
        self.page.wait_for_url(url_pattern, timeout=timeout)

    def query(self, selector: str):
        """Query for an element, returns None if not found."""
        return self.page.query_selector(selector)

    def query_all(self, selector: str) -> list:
        """Query for all matching elements."""
        return self.page.query_selector_all(selector)

    def text_content(self, selector: str) -> str | None:
        """Get text content of an element."""
        el = self.query(selector)
        return el.text_content() if el else None

    def is_visible(self, selector: str) -> bool:
        """Check if an element is visible on the page."""
        return self.page.is_visible(selector)

    def pages(self) -> list:
        """List all open pages/tabs in the context."""
        return self._context.pages if self._context else []

    def switch_to_page(self, index: int) -> None:
        """Switch to a different tab by index."""
        pages = self.pages()
        if 0 <= index < len(pages):
            self._page = pages[index]
            self._page.bring_to_front()

    def new_page(self):
        """Create a new tab."""
        self._page = self._context.new_page()
        return self._page

    def close(self) -> None:
        """Close the browser and clean up."""
        try:
            if self._browser:
                self._browser.close()
                self._browser = None
        except Exception:
            logger.debug("Error closing browser", exc_info=True)
        try:
            if self._context and not self._browser:
                # Persistent context (bundled mode)
                self._context.close()
            self._context = None
        except Exception:
            logger.debug("Error closing browser context", exc_info=True)
        try:
            if self._pw:
                self._pw.stop()
                self._pw = None
        except Exception:
            logger.debug("Error stopping playwright", exc_info=True)
        # Kill Chrome process if we launched it
        if self._chrome_proc and self._chrome_proc.poll() is None:
            _kill_process_tree(self._chrome_proc.pid)
            self._chrome_proc = None
        self._page = None
        logger.info("[worker-%d] Browser closed", self.worker_id)

    def __enter__(self) -> "Browser":
        self.launch()
        return self

    def __exit__(self, *args) -> None:
        self.close()
