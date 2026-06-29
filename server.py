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
    "allow_direct_fallback": os.getenv(
        "NAUKRI_ALLOW_DIRECT_FALLBACK",
        "false" if os.getenv("GITHUB_ACTIONS") == "true" else "true",
    ).lower() == "true",
}

# Schedule times (24-hour)
SCHEDULE_TIMES = ["09:00", "09:40", "13:00", "18:00"]

# Log file
LOG_FILE = os.path.join(SCRIPT_DIR, "resume_update_log.txt")

# Debug output dirs (also referenced by the GitHub Actions workflow)
DEBUG_DIR = os.path.join(tempfile.gettempdir(), "naukri_resume_debug")

# Naukri's edge can reject automated top-level navigations unless these
# browser-like navigation headers are present. They must not be sent to
# scripts/CSS/images though; Chromium may reject those subresource requests.
NAVIGATION_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "upgrade-insecure-requests": "1",
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "sec-fetch-dest": "document",
}

SUBRESOURCE_HEADER_DENYLIST = {
    "accept",
    "upgrade-insecure-requests",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
}

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
# Resume helpers (copied from your server.py)
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
Object.defineProperty(navigator, 'permissions', {
    get: () => ({
        query: (parameters) =>
            Promise.resolve({ state: 'prompt', onchange: null }),
    }),
});
window.navigator.userAgentData = {
    brands: [{ brand: 'Chromium', version: '124' }, { brand: 'Google Chrome', version: '124' }],
    mobile: false,
    platform: 'Windows',
};
"""


async def dismiss_cookie_banner(page):
    """Cookie/consent overlays can silently swallow clicks on the fields behind them."""
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
    """Returns a description if Naukri appears to be showing a CAPTCHA / block page, else None."""
    try:
        content = (await page.content()).lower()
    except Exception:
        content = ""
    try:
        body_text = (await page.text_content("body") or "").lower()
    except Exception:
        body_text = ""

    html_indicators = {
        "unusual activity": "Naukri flagged unusual activity",
        "access denied": "Access denied page detected",
        "request blocked": "Access blocked by Naukri / bot detection",
        "forbidden": "Access forbidden page detected",
        "not authorized": "Access denied page detected",
        "you don't have permission": "Permission denied page detected",
    }
    visible_indicators = {
        "captcha": "CAPTCHA challenge detected",
        "are you a robot": "Bot-check page detected",
        "verify you are human": "Human-verification challenge detected",
    }
    for needle, description in html_indicators.items():
        if needle in content:
            return description
    for needle, description in visible_indicators.items():
        if needle in body_text:
            return description
    return None


async def is_blocked_page(page) -> tuple[bool, str | None]:
    """Detect a blocked / access-denied landing page before trying selectors."""
    try:
        title = (await page.title() or "").lower()
        if any(blocked in title for blocked in ["access denied", "blocked", "forbidden", "error"]):
            return True, f"Blocked page detected by title: {title}"
    except Exception:
        pass

    try:
        body = (await page.text_content("body") or "").lower()
        if any(blocked in body for blocked in ["access denied", "blocked", "forbidden", "request blocked"]):
            return True, "Blocked page detected by body text"
    except Exception:
        pass

    return False, None


async def wait_for_any_selector(page, selectors, timeout=10000):
    """Wait for the first matching element in a CSS selector list."""
    selector = ", ".join(selectors)
    try:
        return await page.wait_for_selector(selector, timeout=timeout)
    except PlaywrightTimeout:
        return None


# ─────────────────────────────────────────────
# Playwright login (copied from your server.py, with stealth + cookie handling)
# ─────────────────────────────────────────────

async def naukri_login(page) -> tuple[bool, str | None]:
    """Returns (success, failure_reason)."""
    try:
        await page.goto("https://www.naukri.com/nlogin/login", timeout=30000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        pass

    blocked, reason = await is_blocked_page(page)
    if blocked:
        return False, reason

    block = await detect_bot_block(page)
    if block:
        return False, block

    await dismiss_cookie_banner(page)

    if "login" not in page.url and "nlogin" not in page.url:
        return True, None

    email_selectors = [
        'input#usernameField',
        'input[placeholder*="Email" i]',
        'input[placeholder*="Username" i]',
        'input[type="email"]',
        'input[name*="user" i]',
    ]
    email_field = await wait_for_any_selector(page, email_selectors, timeout=10000)
    if email_field:
        await email_field.click()
        await email_field.fill(CONFIG["email"])

    password_selectors = [
        'input#passwordField',
        'input[placeholder*="Password" i]',
        'input[type="password"]',
        'input[name*="pass" i]',
    ]
    password_field = await wait_for_any_selector(page, password_selectors, timeout=10000)
    if password_field:
        await password_field.click()
        await password_field.fill(CONFIG["password"])

    if not email_field or not password_field:
        content = (await page.content()).lower()
        if any(term in content for term in ["access denied", "blocked", "forbidden", "request blocked", "not authorized", "you don't have permission"]):
            return False, "Access denied / blocked page detected"
        try:
            root_html_len = await page.locator("#root").evaluate("el => el.innerHTML.length", timeout=1000)
            if root_html_len < 100:
                return False, "Login app did not render (Naukri served a blank SPA shell)"
        except Exception:
            pass
        return False, "Could not find email/password fields (selectors may be outdated)"

    submit_selectors = [
        'button[type="submit"]',
        'button.loginButton',
        'button:has-text("Login")',
        'input[type="submit"]',
    ]
    submit_button = await wait_for_any_selector(page, submit_selectors, timeout=10000)
    if not submit_button:
        return False, "Could not find/click the login submit button"

    try:
        await submit_button.click(no_wait_after=True)
    except Exception as e:
        log.warning(f"Login click triggered a transient navigation error; continuing to wait: {e}")

    try:
        await page.wait_for_url(
            lambda url: "login" not in url and "nlogin" not in url,
            timeout=25000,
        )
    except Exception:
        pass

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    await page.wait_for_timeout(3000)

    block = await detect_bot_block(page)
    if block:
        return False, block

    if "login" in page.url or "nlogin" in page.url:
        return False, "Still on login page after submit (wrong credentials, or page changed)"

    return True, None


async def ensure_logged_in(page) -> tuple[bool, str | None]:
    try:
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=20000)
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        if "mnjuser" in page.url and "login" not in page.url:
            return True, None
    except PlaywrightTimeout:
        pass
    return await naukri_login(page)


# ─────────────────────────────────────────────
# Core resume upload
# ─────────────────────────────────────────────

async def dump_failed_page(page, name: str):
    # Log title/URL/text snippet straight into the run log first - this works
    # even if the screenshot step below fails, and needs no artifact download.
    try:
        title = await page.title()
        log.info(f"[{name}] Page title: {title!r}")
        log.info(f"[{name}] Page URL: {page.url}")
    except Exception as e:
        log.error(f"[{name}] Could not read page title/url: {e}")

    try:
        body_text = await page.text_content("body")
        if body_text:
            snippet = " ".join(body_text.split())[:600]
            log.info(f"[{name}] Page text snippet: {snippet!r}")
        else:
            log.info(f"[{name}] Page body text was empty")
    except Exception as e:
        log.error(f"[{name}] Could not read page body text: {e}")

    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        html_path = os.path.join(DEBUG_DIR, f"{name}.html")
        png_path = os.path.join(DEBUG_DIR, f"{name}.png")
        content = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Saved debug page HTML: {html_path}")
        await page.screenshot(
            path=png_path,
            full_page=False,
            timeout=20000,
            animations="disabled",
        )
        log.info(f"Saved debug page screenshot: {png_path}")
    except Exception as e:
        log.error(f"Failed to save debug screenshot (non-fatal, continuing): {e}")


async def find_file_input(page):
    selectors_to_try = [
        'input[type="file"]',
        'input[name*="resume" i]',
        'input[id*="resume" i]',
        'input[class*="resume" i]',
        'input[name*="upload" i]',
        'input[id*="upload" i]',
        'input[class*="upload" i]',
        'input[accept*="pdf" i]',
        'input[accept*="doc" i]',
        'input[title*="resume" i]',
        'input[placeholder*="resume" i]',
    ]
    for selector in selectors_to_try:
        try:
            file_input = await page.query_selector(selector)
            if file_input:
                log.info(f"Found file input with selector: {selector}")
                return file_input
        except Exception:
            continue

    for frame in page.frames:
        for selector in selectors_to_try:
            try:
                file_input = await frame.query_selector(selector)
                if file_input:
                    log.info(f"Found file input in frame with selector: {selector}")
                    return file_input
            except Exception:
                continue

    return None


async def click_upload_trigger(page):
    upload_selectors = [
        'button:has-text("Upload Resume")',
        'a:has-text("Upload Resume")',
        'button:has-text("Upload")',
        'a:has-text("Upload")',
        'button:has-text("Update Resume")',
        'a:has-text("Update Resume")',
        'button:has-text("Add Resume")',
        'a:has-text("Add Resume")',
        'button:has-text("Change Resume")',
        'a:has-text("Change Resume")',
        '[data-qa-id="resumeUpload"]',
        '[data-testid="resume-upload"]',
        '[class*="upload" i]',
        '[class*="resume" i] button',
        'text="Upload Resume"',
    ]
    for selector in upload_selectors:
        try:
            upload_btn = await page.query_selector(selector)
            if upload_btn:
                log.info(f"Clicking upload trigger selector: {selector}")
                await upload_btn.click()
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


async def launch_naukri_browser(p, use_proxy: bool):
    launch_kwargs = {
        "headless": CONFIG["headless"],
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1920,1080",
        ],
    }
    if use_proxy and CONFIG["proxy_server"]:
        proxy_username = CONFIG["proxy_username"]
        if proxy_username and "-session-" not in proxy_username:
            session_id = uuid.uuid4().hex[:10]
            proxy_username = f"{proxy_username}-session-{session_id}"
            log.info(f"Pinning proxy to a sticky session: ...-session-{session_id}")
        proxy_config = {"server": CONFIG["proxy_server"]}
        if proxy_username:
            proxy_config["username"] = proxy_username
        if CONFIG["proxy_password"]:
            proxy_config["password"] = CONFIG["proxy_password"]
        launch_kwargs["proxy"] = proxy_config
        log.info(f"Using proxy: {CONFIG['proxy_server']}")
    elif use_proxy:
        log.info("Proxy configured but no proxy server is provided; connecting directly")
    else:
        log.info("Connecting directly without proxy")
    return await p.chromium.launch(**launch_kwargs)


async def create_naukri_context(browser):
    return await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        extra_http_headers={
            "accept-language": "en-US,en;q=0.9",
            **NAVIGATION_HEADERS,
        },
        ignore_https_errors=True,
    )


async def install_naukri_request_workaround(page):
    async def sanitize_headers(route):
        request = route.request
        if request.resource_type == "document":
            await route.continue_()
            return

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in SUBRESOURCE_HEADER_DENYLIST
        }
        await route.continue_(headers=headers)

    await page.route("**/*", sanitize_headers)


async def do_resume_upload() -> int:
    """Returns a process exit code: 0 = success/ambiguous-but-ok, 1 = confirmed failure."""
    source_path     = CONFIG["resume_source"]
    filename_format = CONFIG["resume_filename_format"]

    if os.getenv("GITHUB_ACTIONS") == "true" and not CONFIG["proxy_server"]:
        gha_error(
            "GitHub-hosted runners are commonly blocked by Naukri/Akamai. "
            "Set PROXY_SERVER, PROXY_USERNAME, and PROXY_PASSWORD repository "
            "secrets for a residential/static proxy, or run this workflow on a "
            "self-hosted runner from your normal network."
        )
        return 1

    try:
        resume_file = create_daily_resume_copy(source_path, filename_format)
        log.info(f"Resume copy ready: {resume_file}")
    except FileNotFoundError as e:
        gha_error(f"Resume file error: {e}")
        return 1

    async def run_attempt(p, use_proxy: bool) -> int:
        browser = await launch_naukri_browser(p, use_proxy=use_proxy)
        context = await create_naukri_context(browser)
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()
        await install_naukri_request_workaround(page)
        try:
            logged_in, reason = await ensure_logged_in(page)
            if not logged_in:
                gha_error(f"LOGIN FAILED{' (no proxy)' if not use_proxy else ''} - {reason or 'unknown reason'}")
                await dump_failed_page(page, "naukri_login_failure" + ("_no_proxy" if not use_proxy else ""))
                return 1

            await page.goto("https://www.naukri.com/mnjuser/profile", timeout=20000)
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1000)

            block = await detect_bot_block(page)
            if block:
                gha_error(f"BLOCKED AFTER LOGIN{' (no proxy)' if not use_proxy else ''} - {block}")
                await dump_failed_page(page, "naukri_post_login_block" + ("_no_proxy" if not use_proxy else ""))
                return 1

            file_input = await find_file_input(page)
            if not file_input:
                clicked = await click_upload_trigger(page)
                if clicked:
                    file_input = await find_file_input(page)

            if not file_input:
                gha_error("FAILED - Resume upload field not found on Naukri profile page")
                await dump_failed_page(page, "naukri_upload_failure" + ("_no_proxy" if not use_proxy else ""))
                return 1

            await file_input.set_input_files(resume_file)
            await page.wait_for_timeout(1500)

            confirmed = False
            for text in ["resume uploaded", "upload successful", "resume updated", "uploaded successfully"]:
                el = await page.query_selector(f'body:has-text("{text}")')
                if el:
                    confirmed = True
                    break

            fname = os.path.basename(resume_file)
            if confirmed:
                log.info(f"SUCCESS - Resume uploaded: {fname}")
                return 0

            gha_warning(
                f"Uploaded file input was set ({fname}) but no confirmation text was "
                "detected on the page. This may still have worked - check Naukri manually "
                "and update the confirmation-text list if the wording changed."
            )
            await dump_failed_page(page, "naukri_upload_unconfirmed" + ("_no_proxy" if not use_proxy else ""))
            return 0

        except Exception as e:
            gha_error(f"ERROR during upload{' (no proxy)' if not use_proxy else ''}: {e}")
            await dump_failed_page(page, "naukri_upload_exception" + ("_no_proxy" if not use_proxy else ""))
            return 1
        finally:
            await page.close()
            await browser.close()

    async with async_playwright() as p:
        exit_code = await run_attempt(p, use_proxy=True)
        if exit_code == 1 and CONFIG["proxy_server"] and CONFIG["allow_direct_fallback"]:
            log.info("Retrying without proxy after proxy login failure")
            exit_code = await run_attempt(p, use_proxy=False)
        elif exit_code == 1 and CONFIG["proxy_server"]:
            log.info(
                "Skipping direct no-proxy retry. Set NAUKRI_ALLOW_DIRECT_FALLBACK=true "
                "to retry from the runner IP."
            )
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
