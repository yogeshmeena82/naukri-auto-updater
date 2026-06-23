#!/usr/bin/env python3
r"""
Naukri Resume Auto-Updater
Runs 3x daily: 9AM, 1PM, 6PM

SETUP:
  1. Place this file next to your server.py:
        C:\Users\user\Desktop\agent\naukri_auto_update.py

  2. Install dependencies (if not already):
        pip install schedule playwright python-dotenv
        playwright install chromium

  3. Run once to test:
        python naukri_auto_update.py --once

  4. To auto-start on Windows boot:
        - Press Win+R -> shell:startup -> Enter
        - Create shortcut to: start_auto_update.bat

CHANGES IN THIS VERSION
  - Every failure path now exits with a non-zero status code, so a CI run
    that didn't actually update your resume shows RED instead of a false
    green checkmark.
  - Ambiguous "uploaded but couldn't confirm" outcomes still exit 0 (so a
    wording mismatch doesn't break your pipeline) but print a GitHub
    Actions ::warning:: annotation so you'll actually notice it.
  - Added basic anti-bot-detection tweaks (navigator.webdriver override,
    --disable-blink-features=AutomationControlled) since Naukri can show a
    CAPTCHA / block to plain headless browsers, especially from datacenter
    IPs like GitHub-hosted runners.
  - Cookie-consent banner dismissal before login, since an overlay can
    silently eat the click on the email/password fields.
  - Clearer END-OF-RUN summary line so it's the very last thing in the log.
"""

import asyncio
import logging
import os
import sys
import shutil
import tempfile
import schedule
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# Load .env from same folder as this script
# ─────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

# ─────────────────────────────────────────────
# CONFIG — mirrors your server.py CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    "email":    os.getenv("NAUKRI_EMAIL", "your_email@example.com"),
    "password": os.getenv("NAUKRI_PASSWORD", "your_password"),
    "headless": os.getenv("NAUKRI_HEADLESS", "true").lower() == "true",
    "resume_source": os.getenv(
        "NAUKRI_RESUME_SOURCE",
        "./resume"
    ),
    "resume_filename_format": os.getenv(
        "NAUKRI_RESUME_FILENAME_FORMAT",
        "VriseResume_{day}{mon}{year}"
    ),
    # Optional residential proxy, e.g. http://proxy.provider.com:12321
    "proxy_server":   os.getenv("PROXY_SERVER", "").strip(),
    "proxy_username": os.getenv("PROXY_USERNAME", "").strip(),
    "proxy_password": os.getenv("PROXY_PASSWORD", "").strip(),
}

# Schedule times (24-hour)
SCHEDULE_TIMES = ["09:00", "09:40", "13:00", "18:00"]

# Log file
LOG_FILE = os.path.join(SCRIPT_DIR, "resume_update_log.txt")

# Debug output dirs (also referenced by the GitHub Actions workflow)
DEBUG_DIR = os.path.join(tempfile.gettempdir(), "naukri_resume_debug")

# ─────────────────────────────────────────────
# Timeouts — generous to handle slow proxies
# ─────────────────────────────────────────────
GOTO_TIMEOUT      = 90_000   # 90s page load
DOM_TIMEOUT       = 90_000   # 90s wait for domcontentloaded
SELECTOR_TIMEOUT  = 30_000   # 30s per selector wait
BODY_TEXT_TIMEOUT = 15_000   # 15s for body text read

# ─────────────────────────────────────────────
# Logging setup — UTF-8 forced to avoid cp1252 errors on Windows
# ─────────────────────────────────────────────
file_handler   = logging.FileHandler(LOG_FILE, encoding="utf-8")
stream_handler = logging.StreamHandler(sys.stdout)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[file_handler, stream_handler],
)
log = logging.getLogger(__name__)


def gha_warning(message: str):
    """Emit a GitHub Actions warning annotation (no-op outside Actions, just a normal log line)."""
    log.warning(message)
    print(f"::warning::{message}")


def gha_error(message: str):
    """Emit a GitHub Actions error annotation."""
    log.error(message)
    print(f"::error::{message}")


# ─────────────────────────────────────────────
# Resume helpers
# ─────────────────────────────────────────────

def get_daily_resume_filename(source_path: str, filename_format: str = None) -> str:
    ext = os.path.splitext(source_path)[1] or ".pdf"
    fmt = filename_format or CONFIG["resume_filename_format"]
    now = datetime.now()
    values = {
        "day":  f"{now.day:02d}",
        "mon":  now.strftime("%b").lower(),
        "year": str(now.year),
        "date": now.strftime("%d%b%Y").lower(),
    }
    filename = fmt
    for key, value in values.items():
        filename = filename.replace(f"{{{key}}}", value)
    if not os.path.splitext(filename)[1]:
        filename += ext
    return filename


def resolve_resume_source_path(source_path: str) -> str:
    if os.path.isfile(source_path):
        return source_path

    if os.path.isdir(source_path):
        for name in ["resume.pdf", "Resume.pdf", "resume.docx", "Resume.docx"]:
            candidate = os.path.join(source_path, name)
            if os.path.isfile(candidate):
                return candidate
        matches = [
            os.path.join(source_path, f)
            for f in os.listdir(source_path)
            if os.path.splitext(f)[1].lower() in {".pdf", ".docx", ".doc"}
        ]
        if matches:
            return matches[0]
        raise FileNotFoundError(f"No resume file found in: {source_path}")

    return source_path


def create_daily_resume_copy(source_path: str, filename_format: str = None) -> str:
    source_path = resolve_resume_source_path(source_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Resume file not found: {source_path}")
    target_name = get_daily_resume_filename(source_path, filename_format)
    target_dir  = os.path.join(tempfile.gettempdir(), "naukri_resume_uploads")
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, target_name)
    shutil.copyfile(source_path, target_path)
    return target_path


# ─────────────────────────────────────────────
# Bot-detection mitigation
# ─────────────────────────────────────────────

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'product', { get: () => 'Gecko' });
Object.defineProperty(navigator, 'productSub', { get: () => '20030107' });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
window.navigator.userAgentData = {
    brands: [{ brand: 'Chromium', version: '124' }, { brand: 'Google Chrome', version: '124' }],
    mobile: false,
    platform: 'Windows',
};
"""


async def dismiss_cookie_banner(page):
    for selector in [
        'button:has-text("Accept")',
        'button:has-text("I Accept")',
        'button:has-text("Got it")',
        '#wzrk-cancel',
        '.cookie-banner button',
        'button:has-text("Agree")',
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await page.wait_for_timeout(500)
        except Exception:
            continue


async def detect_bot_block(page) -> str | None:
    try:
        content = (await page.content()).lower()
    except Exception:
        return None
    indicators = {
        "captcha": "CAPTCHA challenge detected",
        "unusual activity": "Naukri flagged unusual activity",
        "are you a robot": "Bot-check page detected",
        "access denied": "Access denied page detected",
        "request blocked": "Access blocked by Naukri / bot detection",
        "verify you are human": "Human-verification challenge detected",
        "forbidden": "Access forbidden page detected",
    }
    for needle, description in indicators.items():
        if needle in content:
            return description
    return None


async def wait_for_any_selector(page, selectors, timeout=SELECTOR_TIMEOUT):
    """Wait for the first matching element from a list of selectors."""
    selector = ", ".join(selectors)
    try:
        return await page.wait_for_selector(selector, timeout=timeout, state="visible")
    except PlaywrightTimeout:
        return None


async def safe_body_text(page) -> str:
    """Read body text with a short timeout so a slow proxy doesn't hang indefinitely."""
    try:
        return (await page.text_content("body", timeout=BODY_TEXT_TIMEOUT)) or ""
    except Exception:
        return ""


# ─────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────

async def naukri_login(page) -> tuple[bool, str | None]:
    """Returns (success, failure_reason)."""
    log.info("Navigating to Naukri login page...")
    try:
        await page.goto("https://www.naukri.com/nlogin/login", timeout=GOTO_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Timed out waiting for login page load — continuing anyway")

    # Log what actually loaded so we know immediately if it's a blank/block page
    try:
        title = await page.title()
        log.info(f"Login page loaded. Title: {title!r}  URL: {page.url}")
    except Exception:
        log.warning("Could not read page title after goto")

    # Extra wait for React to render the form
    await page.wait_for_timeout(5000)

    block = await detect_bot_block(page)
    if block:
        return False, block

    await dismiss_cookie_banner(page)

    if "login" not in page.url and "nlogin" not in page.url:
        log.info("Already logged in (redirected away from login page)")
        return True, None

    # ── Email ────────────────────────────────────────────────────────────────
    email_selectors = [
        'input#usernameField',
        'input[placeholder*="Email" i]',
        'input[placeholder*="Username" i]',
        'input[type="email"]',
        'input[name*="user" i]',
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'form input[type="text"]',
    ]
    log.info("Looking for email field...")
    email_field = await wait_for_any_selector(page, email_selectors, timeout=SELECTOR_TIMEOUT)
    if email_field:
        await email_field.click()
        await email_field.fill(CONFIG["email"])
        log.info("Email field filled.")
    else:
        # Dump what inputs exist so we can fix selectors next time
        try:
            inputs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('input')).map(el => ({
                    type: el.type, name: el.name, id: el.id,
                    placeholder: el.placeholder,
                    class: el.className.slice(0, 80)
                }))
            """)
            log.error("Email field NOT found. Inputs currently on page:")
            for inp in inputs:
                log.error(f"  {inp}")
        except Exception as e:
            log.error(f"Could not enumerate inputs: {e}")

    # ── Password ─────────────────────────────────────────────────────────────
    password_selectors = [
        'input#passwordField',
        'input[placeholder*="Password" i]',
        'input[type="password"]',
        'input[name*="pass" i]',
        'input[autocomplete="current-password"]',
    ]
    log.info("Looking for password field...")
    password_field = await wait_for_any_selector(page, password_selectors, timeout=SELECTOR_TIMEOUT)
    if password_field:
        await password_field.click()
        await password_field.fill(CONFIG["password"])
        log.info("Password field filled.")

    if not email_field or not password_field:
        missing = []
        if not email_field:
            missing.append("email")
        if not password_field:
            missing.append("password")
        return False, f"Could not find {' and '.join(missing)} field(s) — check logged inputs above"

    # ── Submit ───────────────────────────────────────────────────────────────
    submit_selectors = [
        'button[type="submit"]',
        'button.loginButton',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'input[type="submit"]',
    ]
    log.info("Looking for submit button...")
    submit_button = await wait_for_any_selector(page, submit_selectors, timeout=SELECTOR_TIMEOUT)
    if not submit_button:
        return False, "Could not find the login submit button"

    await submit_button.click()
    log.info("Submit clicked, waiting for redirect...")

    try:
        await page.wait_for_url(
            lambda url: "login" not in url and "nlogin" not in url,
            timeout=30_000,
        )
    except PlaywrightTimeout:
        pass

    block = await detect_bot_block(page)
    if block:
        return False, block

    if "login" in page.url or "nlogin" in page.url:
        return False, "Still on login page after submit (wrong credentials, or page changed)"

    log.info(f"Login successful. Now at: {page.url}")
    return True, None


async def ensure_logged_in(page) -> tuple[bool, str | None]:
    try:
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=GOTO_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
        if "mnjuser" in page.url and "login" not in page.url:
            log.info("Session already active, skipped login.")
            return True, None
    except PlaywrightTimeout:
        pass
    return await naukri_login(page)


# ─────────────────────────────────────────────
# Core resume upload
# ─────────────────────────────────────────────

async def dump_failed_page(page, name: str):
    try:
        title = await page.title()
        log.info(f"[{name}] Page title: {title!r}")
        log.info(f"[{name}] Page URL: {page.url}")
    except Exception as e:
        log.error(f"[{name}] Could not read page title/url: {e}")

    body_text = await safe_body_text(page)
    if body_text:
        snippet = " ".join(body_text.split())[:600]
        log.info(f"[{name}] Page text snippet: {snippet!r}")
    else:
        log.info(f"[{name}] Page body text was empty or timed out")

    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        html_path = os.path.join(DEBUG_DIR, f"{name}.html")
        png_path  = os.path.join(DEBUG_DIR, f"{name}.png")
        content = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Saved debug HTML: {html_path}")
        await page.screenshot(path=png_path, full_page=False, timeout=15_000, animations="disabled")
        log.info(f"Saved debug screenshot: {png_path}")
    except Exception as e:
        log.error(f"Failed to save debug artifacts (non-fatal): {e}")


async def find_file_input(page):
    selectors_to_try = [
        'input[type="file"]',
        'input[name*="resume" i]',
        'input[id*="resume" i]',
        'input[class*="resume" i]',
        'input[name*="upload" i]',
        'input[id*="upload" i]',
        'input[accept*="pdf" i]',
        'input[accept*="doc" i]',
    ]
    for selector in selectors_to_try:
        try:
            el = await page.query_selector(selector)
            if el:
                log.info(f"Found file input: {selector}")
                return el
        except Exception:
            continue
    for frame in page.frames:
        for selector in selectors_to_try:
            try:
                el = await frame.query_selector(selector)
                if el:
                    log.info(f"Found file input in frame: {selector}")
                    return el
            except Exception:
                continue
    return None


async def click_upload_trigger(page):
    upload_selectors = [
        'button:has-text("Upload Resume")',
        'a:has-text("Upload Resume")',
        'button:has-text("Update Resume")',
        'a:has-text("Update Resume")',
        'button:has-text("Upload")',
        '[data-qa-id="resumeUpload"]',
        '[data-testid="resume-upload"]',
    ]
    for selector in upload_selectors:
        try:
            btn = await page.query_selector(selector)
            if btn:
                log.info(f"Clicking upload trigger: {selector}")
                await btn.click()
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def launch_browser(p, use_proxy: bool):
    launch_kwargs = {
        "headless": CONFIG["headless"],
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
        ],
    }
    if use_proxy and CONFIG["proxy_server"]:
        proxy_username = CONFIG["proxy_username"]
        if proxy_username and "-session-" not in proxy_username:
            session_id = uuid.uuid4().hex[:10]
            proxy_username = f"{proxy_username}-session-{session_id}"
            log.info(f"Proxy sticky session: ...{session_id}")
        proxy_cfg = {"server": CONFIG["proxy_server"]}
        if proxy_username:
            proxy_cfg["username"] = proxy_username
        if CONFIG["proxy_password"]:
            proxy_cfg["password"] = CONFIG["proxy_password"]
        launch_kwargs["proxy"] = proxy_cfg
        log.info(f"Using proxy: {CONFIG['proxy_server']}")
    else:
        log.info("Connecting directly (no proxy)")
    return await p.chromium.launch(**launch_kwargs)


async def create_context(browser):
    return await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        ignore_https_errors=True,
    )


async def run_upload_attempt(p, resume_file: str, use_proxy: bool) -> int:
    label = "proxy" if use_proxy else "direct"
    log.info(f"--- Attempt: {label} ---")

    browser = await launch_browser(p, use_proxy=use_proxy)
    context = await create_context(browser)
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    page = await context.new_page()

    try:
        logged_in, reason = await ensure_logged_in(page)
        if not logged_in:
            gha_error(f"LOGIN FAILED ({label}) - {reason or 'unknown'}")
            await dump_failed_page(page, f"login_failure_{label}")
            return 1

        log.info("Navigating to profile page...")
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=GOTO_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
        await page.wait_for_timeout(2000)

        block = await detect_bot_block(page)
        if block:
            gha_error(f"BLOCKED AFTER LOGIN ({label}) - {block}")
            await dump_failed_page(page, f"post_login_block_{label}")
            return 1

        file_input = await find_file_input(page)
        if not file_input:
            clicked = await click_upload_trigger(page)
            if clicked:
                file_input = await find_file_input(page)

        if not file_input:
            gha_error(f"FAILED ({label}) - Resume upload field not found")
            await dump_failed_page(page, f"upload_failure_{label}")
            return 1

        await file_input.set_input_files(resume_file)
        await page.wait_for_timeout(3000)

        confirmed = False
        for text in ["resume uploaded", "upload successful", "resume updated", "uploaded successfully"]:
            el = await page.query_selector(f'body:has-text("{text}")')
            if el:
                confirmed = True
                break

        fname = os.path.basename(resume_file)
        if confirmed:
            log.info(f"SUCCESS ({label}) - Resume uploaded: {fname}")
            return 0

        gha_warning(
            f"({label}) File input set ({fname}) but no confirmation text detected. "
            "May have worked — check Naukri manually."
        )
        await dump_failed_page(page, f"upload_unconfirmed_{label}")
        return 0

    except Exception as e:
        gha_error(f"ERROR ({label}): {e}")
        await dump_failed_page(page, f"upload_exception_{label}")
        return 1
    finally:
        await page.close()
        await browser.close()


async def do_resume_upload() -> int:
    try:
        resume_file = create_daily_resume_copy(CONFIG["resume_source"], CONFIG["resume_filename_format"])
        log.info(f"Resume copy ready: {resume_file}")
    except FileNotFoundError as e:
        gha_error(f"Resume file error: {e}")
        return 1

    async with async_playwright() as p:
        # Always try direct connection first — it's faster and more reliable
        # than the free Bright Data proxy. Only use proxy if direct is blocked.
        exit_code = await run_upload_attempt(p, resume_file, use_proxy=False)

        if exit_code != 0 and CONFIG["proxy_server"]:
            log.info("Direct attempt failed — retrying via proxy...")
            exit_code = await run_upload_attempt(p, resume_file, use_proxy=True)

        return exit_code


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def run_update() -> int:
    log.info("Scheduled resume update triggered")
    code = asyncio.run(do_resume_upload())
    if code == 0:
        log.info("RUN SUMMARY: resume update completed OK")
    else:
        gha_error("RUN SUMMARY: resume update FAILED - see errors above / debug artifacts")
    return code


def main():
    log.info("=" * 55)
    log.info("  Naukri Resume Auto-Updater STARTED")
    log.info(f"  Schedule: {', '.join(SCHEDULE_TIMES)}")
    log.info(f"  Resume source: {CONFIG['resume_source']}")
    log.info(f"  Log file: {LOG_FILE}")
    log.info("=" * 55)

    for t in SCHEDULE_TIMES:
        schedule.every().day.at(t).do(run_update)
        log.info(f"  Scheduled at {t}")

    log.info("Running initial update now...")
    run_update()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        exit_code = run_update()
        sys.exit(exit_code)
    else:
        main()
