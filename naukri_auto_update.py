#!/usr/bin/env python3
r"""
Naukri Resume Auto-Updater
Runs 4x daily: 9AM, 9:40AM, 1PM, 6PM IST

SETUP:
  1. pip install schedule playwright python-dotenv
     playwright install chromium
  2. python naukri_auto_update.py --once   (test run)
  3. For Windows auto-start: Task Scheduler or shell:startup shortcut

STRATEGY (in order):
  1. Direct (no proxy) — works when GitHub IP is not blocked
  2. Via proxy        — fallback when Akamai blocks the datacenter IP
     Both attempts try the standard login URL first, then the API-based
     login as a secondary fallback.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime

import schedule
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Env ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

CONFIG = {
    "email":    os.getenv("NAUKRI_EMAIL", "your_email@example.com"),
    "password": os.getenv("NAUKRI_PASSWORD", "your_password"),
    "headless": os.getenv("NAUKRI_HEADLESS", "true").lower() == "true",
    "resume_source":          os.getenv("NAUKRI_RESUME_SOURCE", "./resume"),
    "resume_filename_format": os.getenv("NAUKRI_RESUME_FILENAME_FORMAT", "VriseResume_{day}{mon}{year}"),
    "proxy_server":   os.getenv("PROXY_SERVER", "").strip(),
    "proxy_username": os.getenv("PROXY_USERNAME", "").strip(),
    "proxy_password": os.getenv("PROXY_PASSWORD", "").strip(),
}

SCHEDULE_TIMES = ["09:00", "09:40", "13:00", "18:00"]
LOG_FILE  = os.path.join(SCRIPT_DIR, "resume_update_log.txt")
DEBUG_DIR = os.path.join(tempfile.gettempdir(), "naukri_resume_debug")

# Timeouts
NAV_TIMEOUT      = 90_000   # page.goto
DOM_TIMEOUT      = 90_000   # wait for domcontentloaded
SELECTOR_TIMEOUT = 30_000   # wait_for_selector
BODY_TIMEOUT     = 10_000   # text_content("body")

# ── Logging ───────────────────────────────────────────────────────────────────
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


def gha_warning(msg: str):
    log.warning(msg)
    print(f"::warning::{msg}")


def gha_error(msg: str):
    log.error(msg)
    print(f"::error::{msg}")


# ── Resume helpers ────────────────────────────────────────────────────────────

def get_daily_resume_filename(source_path: str, fmt: str = None) -> str:
    ext = os.path.splitext(source_path)[1] or ".pdf"
    fmt = fmt or CONFIG["resume_filename_format"]
    now = datetime.now()
    for key, val in {
        "day": f"{now.day:02d}", "mon": now.strftime("%b").lower(),
        "year": str(now.year), "date": now.strftime("%d%b%Y").lower(),
    }.items():
        fmt = fmt.replace(f"{{{key}}}", val)
    if not os.path.splitext(fmt)[1]:
        fmt += ext
    return fmt


def resolve_resume_path(src: str) -> str:
    if os.path.isfile(src):
        return src
    if os.path.isdir(src):
        for name in ["resume.pdf", "Resume.pdf", "resume.docx", "Resume.docx"]:
            c = os.path.join(src, name)
            if os.path.isfile(c):
                return c
        matches = [
            os.path.join(src, f) for f in os.listdir(src)
            if os.path.splitext(f)[1].lower() in {".pdf", ".docx", ".doc"}
        ]
        if matches:
            return matches[0]
        raise FileNotFoundError(f"No resume file found in: {src}")
    return src


def create_daily_resume_copy(src: str, fmt: str = None) -> str:
    src = resolve_resume_path(src)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Resume file not found: {src}")
    name = get_daily_resume_filename(src, fmt)
    dest_dir = os.path.join(tempfile.gettempdir(), "naukri_resume_uploads")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    shutil.copyfile(src, dest)
    return dest


# ── Stealth ───────────────────────────────────────────────────────────────────

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages',          { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins',            { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'platform',           { get: () => 'Win32' });
Object.defineProperty(navigator, 'vendor',             { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'hardwareConcurrency',{ get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',       { get: () => 8 });
window.navigator.userAgentData = {
    brands: [{ brand: 'Chromium', version: '124' }, { brand: 'Google Chrome', version: '124' }],
    mobile: false, platform: 'Windows',
};
"""


# ── Page helpers ──────────────────────────────────────────────────────────────

async def safe_title(page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def safe_body_text(page) -> str:
    try:
        return (await page.text_content("body", timeout=BODY_TIMEOUT)) or ""
    except Exception:
        return ""


async def safe_content(page) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def detect_block(page) -> str | None:
    content = (await safe_content(page)).lower()
    for needle, desc in {
        "access denied":        "Access denied (Akamai/Naukri firewall)",
        "request blocked":      "Request blocked by Naukri/Akamai",
        "captcha":              "CAPTCHA challenge",
        "unusual activity":     "Unusual activity flag",
        "are you a robot":      "Bot-check page",
        "verify you are human": "Human-verification challenge",
        "forbidden":            "403 Forbidden",
    }.items():
        if needle in content:
            return desc
    return None


async def dismiss_banners(page):
    for sel in [
        'button:has-text("Accept")', 'button:has-text("I Accept")',
        'button:has-text("Got it")', 'button:has-text("Agree")',
        '#wzrk-cancel', '.cookie-banner button',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(400)
        except Exception:
            continue


async def wait_selector(page, selectors: list[str], timeout=SELECTOR_TIMEOUT):
    """Return first matching visible element, or None."""
    combined = ", ".join(selectors)
    try:
        return await page.wait_for_selector(combined, timeout=timeout, state="visible")
    except PlaywrightTimeout:
        return None


async def dump_page(page, tag: str):
    """Log title/URL/body-snippet and save HTML + screenshot."""
    title = await safe_title(page)
    log.info(f"[{tag}] title={title!r}  url={page.url}")
    body = await safe_body_text(page)
    if body:
        log.info(f"[{tag}] body snippet: {' '.join(body.split())[:400]!r}")
    else:
        log.info(f"[{tag}] body empty/timed-out")

    # Log all inputs to help diagnose selector mismatches
    try:
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(el => ({
                type: el.type, id: el.id, name: el.name,
                placeholder: el.placeholder, cls: el.className.slice(0, 60)
            }))
        """)
        if inputs:
            log.info(f"[{tag}] inputs on page ({len(inputs)}):")
            for inp in inputs:
                log.info(f"  {inp}")
        else:
            log.info(f"[{tag}] NO inputs found on page")
    except Exception as e:
        log.info(f"[{tag}] could not enumerate inputs: {e}")

    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        html_path = os.path.join(DEBUG_DIR, f"{tag}.html")
        png_path  = os.path.join(DEBUG_DIR, f"{tag}.png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(await safe_content(page))
        log.info(f"[{tag}] HTML saved: {html_path}")
        await page.screenshot(path=png_path, full_page=False, timeout=12_000, animations="disabled")
        log.info(f"[{tag}] screenshot saved: {png_path}")
    except Exception as e:
        log.warning(f"[{tag}] debug artifact save failed (non-fatal): {e}")


# ── Login via API (bypass the React login page entirely) ─────────────────────

async def api_login(page) -> tuple[bool, str | None]:
    """
    POST to Naukri's internal login API, then set cookies directly.
    Avoids the React-rendered login form that breaks under slow proxies.
    """
    log.info("Trying API-based login...")
    try:
        response = await page.request.post(
            "https://www.naukri.com/central-login-services/v1/login",
            headers={
                "Content-Type":    "application/json",
                "Accept":          "application/json",
                "appid":           "109",
                "systemid":        "Naukri",
                "clientid":        "d3skt0p",
                "gaid":            "",
                "referer":         "https://www.naukri.com/",
            },
            data=json.dumps({
                "username": CONFIG["email"],
                "password": CONFIG["password"],
                "type":     "login",
            }),
            timeout=60_000,
        )
        status = response.status
        log.info(f"API login response status: {status}")

        if status == 200:
            body = await response.json()
            log.info(f"API login response: {json.dumps(body)[:300]}")
            # Navigate to profile to let session cookies settle
            await page.goto("https://www.naukri.com/mnjuser/profile", timeout=NAV_TIMEOUT)
            await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
            if "mnjuser" in page.url and "login" not in page.url:
                log.info("API login succeeded.")
                return True, None
            return False, f"API login 200 but still redirected to login (url={page.url})"

        body_text = await response.text()
        return False, f"API login HTTP {status}: {body_text[:200]}"

    except Exception as e:
        return False, f"API login exception: {e}"


# ── Browser login (React form) ────────────────────────────────────────────────

async def browser_login(page) -> tuple[bool, str | None]:
    """Fill the React login form. Requires the page to fully render."""
    log.info("Navigating to Naukri login page...")
    try:
        await page.goto("https://www.naukri.com/nlogin/login", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
    except PlaywrightTimeout:
        log.warning("Timed out waiting for login page — continuing anyway")

    title = await safe_title(page)
    log.info(f"Login page loaded. title={title!r}  url={page.url}")

    # Wait extra for React to hydrate
    await page.wait_for_timeout(5000)

    block = await detect_block(page)
    if block:
        return False, block

    await dismiss_banners(page)

    if "login" not in page.url and "nlogin" not in page.url:
        log.info("Redirected away from login — already logged in.")
        return True, None

    # Check if the page actually has content
    body = await safe_body_text(page)
    if not body or len(body.strip()) < 50:
        log.warning("Page body is empty/blank — page likely did not load over proxy")
        await dump_page(page, "blank_login_page")
        return False, "Login page loaded blank (proxy too slow or blocked)"

    email_field = await wait_selector(page, [
        'input#usernameField',
        'input[placeholder*="Email" i]',
        'input[placeholder*="Username" i]',
        'input[type="email"]',
        'input[name*="user" i]',
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'form input[type="text"]',
    ])

    password_field = await wait_selector(page, [
        'input#passwordField',
        'input[placeholder*="Password" i]',
        'input[type="password"]',
        'input[name*="pass" i]',
        'input[autocomplete="current-password"]',
    ])

    if not email_field or not password_field:
        await dump_page(page, "login_fields_not_found")
        missing = [n for n, f in [("email", email_field), ("password", password_field)] if not f]
        return False, f"Could not find {' and '.join(missing)} field(s) — inputs logged above"

    await email_field.click()
    await email_field.fill(CONFIG["email"])
    await password_field.click()
    await password_field.fill(CONFIG["password"])
    log.info("Credentials filled.")

    submit = await wait_selector(page, [
        'button[type="submit"]', 'button.loginButton',
        'button:has-text("Login")', 'button:has-text("Sign in")',
        'input[type="submit"]',
    ])
    if not submit:
        return False, "Could not find the login submit button"

    await submit.click()
    log.info("Submit clicked, waiting for redirect...")

    try:
        await page.wait_for_url(
            lambda url: "login" not in url and "nlogin" not in url,
            timeout=30_000,
        )
    except PlaywrightTimeout:
        pass

    block = await detect_block(page)
    if block:
        return False, block

    if "login" in page.url or "nlogin" in page.url:
        return False, "Still on login page after submit (wrong credentials or page changed)"

    log.info(f"Browser login succeeded. Now at: {page.url}")
    return True, None


async def ensure_logged_in(page) -> tuple[bool, str | None]:
    # Check if already logged in
    try:
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
        if "mnjuser" in page.url and "login" not in page.url:
            block = await detect_block(page)
            if not block:
                log.info("Session already active.")
                return True, None
    except PlaywrightTimeout:
        pass

    # Try API login first (more reliable under slow proxies)
    ok, reason = await api_login(page)
    if ok:
        return True, None
    log.warning(f"API login failed ({reason}), trying browser login...")

    # Fallback to browser form
    return await browser_login(page)


# ── Upload ────────────────────────────────────────────────────────────────────

async def find_file_input(page):
    for sel in [
        'input[type="file"]', 'input[name*="resume" i]', 'input[id*="resume" i]',
        'input[accept*="pdf" i]', 'input[accept*="doc" i]',
        'input[name*="upload" i]', 'input[id*="upload" i]',
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                log.info(f"File input found: {sel}")
                return el
        except Exception:
            continue
    for frame in page.frames:
        for sel in ['input[type="file"]', 'input[accept*="pdf" i]']:
            try:
                el = await frame.query_selector(sel)
                if el:
                    log.info(f"File input found in frame: {sel}")
                    return el
            except Exception:
                continue
    return None


async def click_upload_trigger(page):
    for sel in [
        'button:has-text("Upload Resume")', 'a:has-text("Upload Resume")',
        'button:has-text("Update Resume")', 'a:has-text("Update Resume")',
        'button:has-text("Upload")', '[data-qa-id="resumeUpload"]',
        '[data-testid="resume-upload"]',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                log.info(f"Upload trigger clicked: {sel}")
                await btn.click()
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


# ── Browser factory ───────────────────────────────────────────────────────────

async def make_browser(p, use_proxy: bool):
    args = [
        "--no-sandbox", "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage", "--window-size=1920,1080",
    ]
    kwargs = {"headless": CONFIG["headless"], "args": args}

    if use_proxy and CONFIG["proxy_server"]:
        uname = CONFIG["proxy_username"]
        if uname and "-session-" not in uname:
            sid = uuid.uuid4().hex[:10]
            uname = f"{uname}-session-{sid}"
            log.info(f"Proxy sticky session: ...{sid}")
        proxy = {"server": CONFIG["proxy_server"]}
        if uname:
            proxy["username"] = uname
        if CONFIG["proxy_password"]:
            proxy["password"] = CONFIG["proxy_password"]
        kwargs["proxy"] = proxy
        log.info(f"Using proxy: {CONFIG['proxy_server']}")
    else:
        log.info("Connecting directly (no proxy)")

    return await p.chromium.launch(**kwargs)


async def make_context(browser):
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


# ── Main attempt ──────────────────────────────────────────────────────────────

async def attempt(p, resume_file: str, use_proxy: bool) -> int:
    label = "proxy" if use_proxy else "direct"
    log.info(f"--- Attempt: {label} ---")

    browser = await make_browser(p, use_proxy)
    context = await make_context(browser)
    await context.add_init_script(STEALTH_SCRIPT)
    page = await context.new_page()

    try:
        ok, reason = await ensure_logged_in(page)
        if not ok:
            gha_error(f"LOGIN FAILED ({label}): {reason}")
            await dump_page(page, f"login_failure_{label}")
            return 1

        log.info("Navigating to profile page for upload...")
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT)
        await page.wait_for_timeout(2000)

        block = await detect_block(page)
        if block:
            gha_error(f"BLOCKED after login ({label}): {block}")
            await dump_page(page, f"post_login_block_{label}")
            return 1

        file_input = await find_file_input(page)
        if not file_input:
            if await click_upload_trigger(page):
                file_input = await find_file_input(page)

        if not file_input:
            gha_error(f"FAILED ({label}): resume upload field not found")
            await dump_page(page, f"upload_failure_{label}")
            return 1

        await file_input.set_input_files(resume_file)
        await page.wait_for_timeout(3000)

        confirmed = any([
            await page.query_selector(f'body:has-text("{t}")')
            for t in ["resume uploaded", "upload successful", "resume updated", "uploaded successfully"]
        ])

        fname = os.path.basename(resume_file)
        if confirmed:
            log.info(f"SUCCESS ({label}): {fname}")
            return 0

        gha_warning(
            f"({label}) File input set ({fname}) but no confirmation text found. "
            "Check Naukri manually — may still have worked."
        )
        await dump_page(page, f"upload_unconfirmed_{label}")
        return 0

    except Exception as e:
        gha_error(f"ERROR ({label}): {e}")
        await dump_page(page, f"exception_{label}")
        return 1
    finally:
        await page.close()
        await browser.close()


async def do_upload() -> int:
    try:
        resume_file = create_daily_resume_copy(CONFIG["resume_source"], CONFIG["resume_filename_format"])
        log.info(f"Resume ready: {resume_file}")
    except FileNotFoundError as e:
        gha_error(f"Resume file error: {e}")
        return 1

    async with async_playwright() as p:
        # Direct first (fast, works when GitHub IP not blocked)
        code = await attempt(p, resume_file, use_proxy=False)
        if code != 0 and CONFIG["proxy_server"]:
            log.info("Direct failed — retrying via proxy...")
            code = await attempt(p, resume_file, use_proxy=True)
        return code


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_update() -> int:
    log.info("Resume update triggered")
    code = asyncio.run(do_upload())
    if code == 0:
        log.info("RUN SUMMARY: OK")
    else:
        gha_error("RUN SUMMARY: FAILED — see errors above / debug artifacts")
    return code


def main():
    log.info("=" * 55)
    log.info("  Naukri Resume Auto-Updater STARTED")
    log.info(f"  Schedule: {', '.join(SCHEDULE_TIMES)}")
    log.info(f"  Resume: {CONFIG['resume_source']}")
    log.info(f"  Log: {LOG_FILE}")
    log.info("=" * 55)
    for t in SCHEDULE_TIMES:
        schedule.every().day.at(t).do(run_update)
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
        sys.exit(run_update())
    else:
        main()
