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
        python naukri_auto_update.py

  4. To auto-start on Windows boot:
        - Press Win+R -> shell:startup -> Enter
        - Create shortcut to: start_auto_update.bat
"""

import asyncio
import logging
import os
import sys
import shutil
import tempfile
import schedule
import time
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
}

# Schedule times (24-hour)
SCHEDULE_TIMES = ["09:00",  "09:40", "13:00", "18:00"]

# Log file
LOG_FILE = os.path.join(SCRIPT_DIR, "resume_update_log.txt")

# ─────────────────────────────────────────────
# Logging setup — UTF-8 forced to avoid cp1252 errors on Windows
# ─────────────────────────────────────────────
file_handler   = logging.FileHandler(LOG_FILE, encoding="utf-8")
stream_handler = logging.StreamHandler(sys.stdout)

# Force stdout to utf-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[file_handler, stream_handler],
)
log = logging.getLogger(__name__)


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

    if os.path.exists(source_path) and os.path.isdir(source_path):
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
# Playwright login (copied from your server.py)
# ─────────────────────────────────────────────

async def naukri_login(page) -> bool:
    try:
        await page.goto("https://www.naukri.com/nlogin/login", timeout=30000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        pass

    if "login" not in page.url and "nlogin" not in page.url:
        return True

    for selector in [
        'input[placeholder="Enter your active Email ID / Username"]',
        'input[type="email"]', 'input[name="username"]', '#usernameField',
    ]:
        try:
            field = await page.wait_for_selector(selector, timeout=5000)
            await field.click()
            await field.fill(CONFIG["email"])
            break
        except PlaywrightTimeout:
            continue

    for selector in [
        'input[placeholder="Enter your password"]',
        'input[type="password"]', 'input[name="password"]', '#passwordField',
    ]:
        try:
            field = await page.wait_for_selector(selector, timeout=5000)
            await field.click()
            await field.fill(CONFIG["password"])
            break
        except PlaywrightTimeout:
            continue

    for selector in [
        'button[type="submit"]', 'button.loginButton',
        'button:has-text("Login")', 'input[type="submit"]',
    ]:
        try:
            btn = await page.wait_for_selector(selector, timeout=5000)
            if btn:
                await btn.click()
                break
        except PlaywrightTimeout:
            continue

    await page.wait_for_timeout(2000)
    try:
        await page.wait_for_url(
            lambda url: "login" not in url and "nlogin" not in url,
            timeout=15000
        )
    except PlaywrightTimeout:
        pass

    return "login" not in page.url and "nlogin" not in page.url


async def ensure_logged_in(page) -> bool:
    try:
        await page.goto("https://www.naukri.com/mnjuser/profile", timeout=20000)
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        if "mnjuser" in page.url and "login" not in page.url:
            return True
    except PlaywrightTimeout:
        pass
    return await naukri_login(page)


# ─────────────────────────────────────────────
# Core resume upload
# ─────────────────────────────────────────────

async def do_resume_upload():
    source_path     = CONFIG["resume_source"]
    filename_format = CONFIG["resume_filename_format"]

    try:
        resume_file = create_daily_resume_copy(source_path, filename_format)
        log.info(f"Resume copy ready: {resume_file}")
    except FileNotFoundError as e:
        log.error(f"Resume file error: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=CONFIG["headless"],
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        try:
            logged_in = await ensure_logged_in(page)
            if not logged_in:
                log.error("LOGIN FAILED - check NAUKRI_EMAIL / NAUKRI_PASSWORD in .env")
                return

            await page.goto("https://www.naukri.com/mnjuser/profile")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)

            file_input = None
            for selector in [
                'input[type="file"]',
                'input[name*="resume"]',
                'input[id*="resume"]',
                'input[class*="resume"]',
            ]:
                file_input = await page.query_selector(selector)
                if file_input:
                    break

            if not file_input:
                upload_btn = await page.query_selector('button:has-text("Upload Resume")')
                if upload_btn:
                    await upload_btn.click()
                    await page.wait_for_timeout(1000)
                    for selector in ['input[type="file"]', 'input[name*="resume"]']:
                        file_input = await page.query_selector(selector)
                        if file_input:
                            break

            if not file_input:
                log.error("FAILED - Resume upload field not found on Naukri profile page")
                return

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
                log.info(f"SUCCESS - Resume uploaded: {fname}")
            else:
                log.info(f"DONE - Resume uploaded (no confirmation text detected): {fname}")

        except Exception as e:
            log.error(f"ERROR during upload: {e}")
        finally:
            await page.close()
            await browser.close()


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def run_update():
    log.info("Scheduled resume update triggered")
    asyncio.run(do_resume_upload())


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


import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        asyncio.run(do_resume_upload())
    else:
        main()
