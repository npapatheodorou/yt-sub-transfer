import csv
import os
import time
import threading
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterable, Tuple

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# ---------------------------------------------------------------------
# Paths & basic config
# ---------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

@dataclass(frozen=True)
class Config:
    csv_file: Path = SCRIPT_DIR / "subscriptions.csv"
    log_file: Path = SCRIPT_DIR / "subscription_log.txt"
    skipped_file: Path = SCRIPT_DIR / "skipped_channels.csv"
    offset_file: Path = SCRIPT_DIR / "last_offset.txt"
    auth_file: Path = SCRIPT_DIR / "auth.json"

    wait_secs: float = 10.0
    throttle_secs: float = 1.5
    restart_every_n: int = 25
    retries_per_channel: int = 0  # keeping parity with the original behavior
    login_wait_secs: int = int(os.environ.get("YT_LOGIN_WAIT_SECS", "600"))
    headless_work: bool = True

    chromium_args: tuple = ("--mute-audio",)

CFG = Config()

INIT_SCRIPT = """
(() => {
  const stopAll = () => {
    document.querySelectorAll('video').forEach(v => {
      try { v.muted = true; v.pause(); v.currentTime = 0; } catch {}
    });
  };
  stopAll();
  new MutationObserver(stopAll).observe(document.documentElement, {subtree:true, childList:true});
})();
"""

YOUTUBE_HOME = "https://www.youtube.com/"

SUBSCRIBE_SELECTORS = (
    "button:has-text('Subscribe')",
    "yt-button-shape button:has(span:has-text('Subscribe'))",
    "button[aria-label*='Subscribe']",
    "//button[normalize-space()='Subscribe']",
)

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ---------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------

def read_offset(path: Path = CFG.offset_file) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0

def write_offset(i: int, path: Path = CFG.offset_file) -> None:
    path.write_text(str(i), encoding="utf-8")

def iter_csv_rows(path: Path) -> Iterable[Tuple[int, dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            yield idx, row

def click_consent_if_present(page) -> None:
    selectors = (
        "button:has-text('I agree')",
        "button:has-text('Accept all')",
        "#introAgreeButton",
        "form[action*='consent'] button[type='submit']",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click()
                page.wait_for_timeout(300)
                return
        except Exception:
            continue

def any_page_has_avatar(ctx) -> bool:
    for p in ctx.pages:
        try:
            if p.locator("#avatar-btn").first.is_visible():
                return True
        except Exception:
            continue
    return False

def wait_until_logged_in(ctx, timeout_secs: int) -> bool:
    """
    Wait until an avatar is visible on any page or the user hits Enter.
    """
    user_done = {"flag": False}

    def wait_for_enter():
        input("Press Enter here once you've finished logging in...\n")
        user_done["flag"] = True

    threading.Thread(target=wait_for_enter, daemon=True).start()

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if "youtube.com" not in (page.url or ""):
        page.goto(YOUTUBE_HOME, wait_until="domcontentloaded")
        click_consent_if_present(page)

    start = time.time()
    while (time.time() - start) < timeout_secs:
        for p in ctx.pages:
            click_consent_if_present(p)
        if user_done["flag"]:
            return True
        if any_page_has_avatar(ctx):
            return True
        time.sleep(1.0)

    return False

# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

def ensure_auth_and_get_state_file(p) -> Path:
    """
    Use existing storage_state if valid; otherwise open a visible window
    for manual login, then save to disk.
    """
    if CFG.auth_file.exists():
        logging.info("Checking existing auth.json...")
        browser = p.chromium.launch(headless=True, args=list(CFG.chromium_args))
        ctx = browser.new_context(storage_state=str(CFG.auth_file))
        page = ctx.new_page()
        page.add_init_script(INIT_SCRIPT)
        page.goto(YOUTUBE_HOME, wait_until="domcontentloaded")
        click_consent_if_present(page)
        ok = any_page_has_avatar(ctx)
        ctx.close(); browser.close()
        if ok:
            logging.info("Existing auth.json is valid.")
            return CFG.auth_file
        else:
            logging.info("Existing auth.json invalid; removing.")
            try:
                CFG.auth_file.unlink(missing_ok=True)
            except Exception:
                pass

    logging.info("No valid auth. Opening a visible window for login...")
    browser = p.chromium.launch(headless=False, args=list(CFG.chromium_args))
    ctx = browser.new_context()
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)
    page.goto(YOUTUBE_HOME, wait_until="domcontentloaded")
    click_consent_if_present(page)

    logging.info("Complete login in the opened window. You can press Enter here when done.")
    if not wait_until_logged_in(ctx, CFG.login_wait_secs):
        ctx.close(); browser.close()
        raise SystemExit("Login not detected in time. Re-run and try again.")

    if not any_page_has_avatar(ctx):
        ctx.close(); browser.close()
        raise SystemExit("Avatar not detected; not saving state. Please retry login.")

    page.wait_for_timeout(500)
    ctx.storage_state(path=str(CFG.auth_file))
    logging.info("Saved login to %s", CFG.auth_file)

    ctx.close(); browser.close()
    return CFG.auth_file

# ---------------------------------------------------------------------
# Subscribe helpers
# ---------------------------------------------------------------------

class SubscribeResult:
    __slots__ = ("ok", "reason")
    def __init__(self, ok: bool, reason: Optional[str] = None):
        self.ok = ok
        self.reason = reason

def subscribe_once(page, channel_title: str, channel_url: str) -> SubscribeResult:
    try:
        page.goto(channel_url, wait_until="domcontentloaded")
        click_consent_if_present(page)
    except Exception as e:
        return SubscribeResult(False, f"navigation: {e}")

    try:
        btn = None
        for sel in SUBSCRIBE_SELECTORS:
            candidate = page.locator(sel).first
            try:
                candidate.wait_for(state="visible", timeout=int(CFG.wait_secs * 1000))
                btn = candidate
                break
            except Exception:
                continue

        if not btn:
            return SubscribeResult(False, "subscribe button not found")

        try:
            page.evaluate("el => el.scrollIntoView({block:'center'})", btn)
        except Exception:
            pass

        page.wait_for_timeout(250)
        btn.click()
        return SubscribeResult(True)
    except Exception as e:
        return SubscribeResult(False, f"click: {e}")

# ---------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------

def run_worker_with_state(p, storage_state_path: Path) -> None:
    with CFG.log_file.open("a", encoding="utf-8") as log_fp, \
         CFG.skipped_file.open("a", newline="", encoding="utf-8") as skipped_fp:

        skipped_writer = csv.writer(skipped_fp)

        with CFG.csv_file.open(newline="", encoding="utf-8") as f:
            total = sum(1 for _ in csv.DictReader(f))

        processed_since_restart = 0
        start_index = read_offset()

        browser = p.chromium.launch(headless=CFG.headless_work, args=list(CFG.chromium_args))
        ctx = browser.new_context(storage_state=str(storage_state_path))
        page = ctx.new_page()
        page.add_init_script(INIT_SCRIPT)

        try:
            for idx, row in iter_csv_rows(CFG.csv_file):
                if idx < start_index:
                    continue

                channel_url = (row.get("Channel Url") or "").strip()
                channel_title = (row.get("Channel Title") or "").strip() or channel_url
                logging.info("[%d/%d] %s (%s)", idx + 1, total, channel_title, channel_url or "no url")

                if not channel_url:
                    logging.warning("Missing URL; skipping row.")
                    skipped_writer.writerow([channel_title, channel_url, "missing url"])
                    write_offset(idx + 1)
                    continue

                result = subscribe_once(page, channel_title, channel_url)
                if result.ok:
                    logging.info("Subscribed to %s", channel_title)
                    log_fp.write(f"Subscribed: {channel_title} ({channel_url})\n")
                else:
                    logging.warning("Failed: %s — %s", result.reason, channel_title)
                    skipped_writer.writerow([channel_title, channel_url, result.reason or "unknown"])

                write_offset(idx + 1)
                time.sleep(CFG.throttle_secs)

                processed_since_restart += 1
                if processed_since_restart >= CFG.restart_every_n:
                    logging.info("Restarting browser to clear state/memory...")
                    try:
                        ctx.close(); browser.close()
                    except Exception:
                        pass
                    browser = p.chromium.launch(headless=CFG.headless_work, args=list(CFG.chromium_args))
                    ctx = browser.new_context(storage_state=str(storage_state_path))
                    page = ctx.new_page()
                    page.add_init_script(INIT_SCRIPT)
                    processed_since_restart = 0
        finally:
            try:
                ctx.close(); browser.close()
            except Exception:
                pass

    logging.info("Done. All channels processed.")

# ---------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------

def main() -> None:
    if not CFG.csv_file.exists():
        raise SystemExit(f"CSV file not found: {CFG.csv_file}")

    with sync_playwright() as p:
        state_file = ensure_auth_and_get_state_file(p)
        run_worker_with_state(p, state_file)

if __name__ == "__main__":
    main()