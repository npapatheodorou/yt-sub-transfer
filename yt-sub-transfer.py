#!/usr/bin/env python3
import argparse
import csv
import os
import time
import threading
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Iterable, Tuple, List, Pattern
import re

from playwright.sync_api import sync_playwright, Locator, Page

# ---------------------------------------------------------------------
# Paths & config
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
    retries_per_channel: int = 0
    login_wait_secs: int = int(os.environ.get("YT_LOGIN_WAIT_SECS", "600"))
    headless_work: bool = True
    slowmo_ms: int = 0
    devtools: bool = False

    chromium_args: tuple = ("--mute-audio",)

    # NEW: tighter waits for first role attempt to avoid 10s stalls
    role_first_try_ms: int = 1200

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

# Subscribe / Subscribed
SUBSCRIBE_SELECTORS = (
    "button:has-text('Subscribe')",
    "yt-button-shape button:has(span:has-text('Subscribe'))",
    "button[aria-label*='Subscribe']",
    "//button[normalize-space()='Subscribe']",
)
SUBSCRIBED_SELECTORS = (
    "yt-button-shape button:has(span:has-text('Subscribed'))",
    "button:has-text('Subscribed')",
    "button[aria-label*='Subscribed']",
)

# Dropdown/caret that opens the menu on the pill
SUBSCRIBED_DROPDOWN_SELECTORS = (
    "yt-button-shape[aria-haspopup='menu'] button:has(span:has-text('Subscribed'))",
    "yt-button-shape:has(button:has(span:has-text('Subscribed'))) + yt-button-shape button",
)

# Bell button (if separate)
NOTIF_BELL_SELECTORS = (
    "button[aria-label*='notifications']",
    "button[aria-label*='Notification settings']",
    "button[aria-label*='Notify']",
    "yt-subscribe-button-shape + yt-button-shape button",
    "ytd-subscribe-button-renderer tp-yt-paper-tooltip ~ yt-button-shape button",
)

def build_none_selectors(labels: List[str]) -> dict:
    """
    Build selectors for the 'None' radio item:
      - role(name) regex + role(has_text) filter
      - CSS role scan
      - text-based
      - absolute XPath fallback (last resort)
    """
    joined = "|".join(re.escape(lbl.strip()) for lbl in labels if lbl.strip())
    role_regex: Pattern[str] = re.compile(rf"^(?:{joined})$", re.I)

    text_css = []
    for lbl in labels:
        lbl = lbl.strip()
        if not lbl:
            continue
        text_css.extend((
            f"tp-yt-paper-item:has-text('{lbl}')",
            f"ytd-menu-service-item-renderer:has-text('{lbl}')",
            f"//yt-formatted-string[normalize-space()='{lbl}']",
            # radio-shape label with text
            f"radio-shape label:has-text('{lbl}')",
        ))

    return {
        "role_regex": role_regex,
        "role_css": "[role='menuitemradio']",
        "text_css": tuple(text_css),
        # from your screenshot (index can vary; keep as last resort)
        "xpath_fallback": '//*[@id="contentWrapper"]/yt-sheet-view-model/yt-contextual-sheet-layout/div[2]/yt-list-view-model/yt-list-item-view-model[3]/radio-shape/label',
    }

# set in main()
NONE_SELECTORS = {}

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ---------------------------------------------------------------------
# Utilities
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

def click_consent_if_present(page: Page) -> None:
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
        if user_done["flag"] or any_page_has_avatar(ctx):
            return True
        time.sleep(1.0)
    return False

# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

def ensure_auth_and_get_state_file(p, headless_for_check: bool) -> Path:
    if CFG.auth_file.exists():
        logging.info("Checking existing auth.json...")
        browser = p.chromium.launch(headless=headless_for_check, args=list(CFG.chromium_args))
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
        logging.info("Existing auth.json invalid; removing.")
        try: CFG.auth_file.unlink(missing_ok=True)
        except Exception: pass

    logging.info("No valid auth. Opening a visible window for login...")
    browser = p.chromium.launch(
        headless=False,  # login headful
        args=list(CFG.chromium_args),
        devtools=CFG.devtools,
        slow_mo=CFG.slowmo_ms or None,
    )
    ctx = browser.new_context()
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)
    page.goto(YOUTUBE_HOME, wait_until="domcontentloaded")
    click_consent_if_present(page)

    logging.info("Complete login in the opened window. You can press Enter here when done.")
    if not wait_until_logged_in(ctx, CFG.login_wait_secs):
        ctx.close(); browser.close()
        raise SystemExit("Login not detected in time.")

    if not any_page_has_avatar(ctx):
        ctx.close(); browser.close()
        raise SystemExit("Avatar not detected; not saving state.")

    page.wait_for_timeout(500)
    ctx.storage_state(path=str(CFG.auth_file))
    logging.info("Saved login to %s", CFG.auth_file)
    ctx.close(); browser.close()
    return CFG.auth_file

# ---------------------------------------------------------------------
# Subscribe / Notifications
# ---------------------------------------------------------------------

class SubscribeResult:
    __slots__ = ("ok", "reason")
    def __init__(self, ok: bool, reason: Optional[str] = None):
        self.ok = ok
        self.reason = reason

def _scroll_into_view(locator: Locator, timeout_ms: int = None):
    try:
        locator.scroll_into_view_if_needed(timeout=timeout_ms or int(CFG.wait_secs * 1000))
        locator.page.wait_for_timeout(80)
    except Exception:
        pass

def get_menu_root(page: Page) -> Locator:
    """Return the active menu / contextual sheet container."""
    candidates = page.locator(
        "yt-sheet-view-model, ytd-menu-popup-renderer, "
        "tp-yt-paper-menu-button[opened], tp-yt-iron-dropdown:not([aria-hidden='true'])"
    )
    # pick the first visible one
    for i in range(min(5, candidates.count() or 1)):
        try:
            root = candidates.nth(i)
            if root.is_visible():
                return root
        except Exception:
            continue
    # fallback to page (unscoped)
    return page.locator(":root")

def menu_is_open(page: Page) -> bool:
    root = get_menu_root(page)
    try:
        if root.locator("[role='menuitemradio']").first.is_visible():
            return True
        if root.locator("radio-shape label").first.is_visible():
            return True
    except Exception:
        pass
    return False

def open_notifications_menu(page: Page) -> bool:
    """Open bell OR subscribed dropdown to reveal All/Personalized/None."""
    if menu_is_open(page):
        return True

    # Try the bell
    for sel in NOTIF_BELL_SELECTORS:
        try:
            bell = page.locator(sel).first
            bell.wait_for(state="visible", timeout=int(CFG.wait_secs * 1000))
            _scroll_into_view(bell)
            bell.click()
            page.wait_for_timeout(500)  # time to mount
            if menu_is_open(page):
                return True
        except Exception:
            continue

    # Try the Subscribed pill / dropdown
    for sel in SUBSCRIBED_DROPDOWN_SELECTORS + SUBSCRIBED_SELECTORS:
        try:
            sub_btn = page.locator(sel).first
            sub_btn.wait_for(state="visible", timeout=int(CFG.wait_secs * 1000))
            _scroll_into_view(sub_btn)
            sub_btn.click()
            page.wait_for_timeout(600)
            if menu_is_open(page):
                return True
        except Exception:
            continue

    return False

def _click_none_by_role(page: Page) -> bool:
    """Fast attempts using ARIA role; keep short timeouts so we fall back quickly."""
    try:
        # 1) Filter by text on role (works even if accessible name is odd)
        elem = page.get_by_role("menuitemradio").filter(has_text=NONE_SELECTORS["role_regex"]).first
        elem.wait_for(state="visible", timeout=CFG.role_first_try_ms)
        _scroll_into_view(elem)
        elem.click()
        page.wait_for_timeout(200)
        return True
    except Exception as e:
        logging.debug("role(has_text=regex) failed: %s", e)

    try:
        # 2) Classic role name regex (short timeout)
        elem = page.get_by_role("menuitemradio", name=NONE_SELECTORS["role_regex"]).first
        elem.wait_for(state="visible", timeout=CFG.role_first_try_ms)
        _scroll_into_view(elem)
        elem.click()
        page.wait_for_timeout(200)
        return True
    except Exception as e:
        logging.debug("role(name=regex) failed: %s", e)

    return False

def _click_none_by_css_scan(page: Page) -> bool:
    try:
        role_css = NONE_SELECTORS["role_css"]
        re_pat: Pattern[str] = NONE_SELECTORS["role_regex"]
        root = get_menu_root(page)
        candidates = root.locator(role_css)
        count = candidates.count()
        for i in range(min(20, max(1, count))):
            item = candidates.nth(i)
            try:
                text = item.inner_text(timeout=500).strip()
            except Exception:
                continue
            if re_pat.search(text or ""):
                _scroll_into_view(item)
                try:
                    item.click()
                except Exception:
                    item.click(force=True)
                page.wait_for_timeout(200)
                return True
    except Exception as e:
        logging.debug("css role scan failed: %s", e)
    return False

def set_notifications_none(page: Page) -> SubscribeResult:
    if not open_notifications_menu(page):
        return SubscribeResult(False, "notif menu not found")

    # Scoped to the open menu to avoid stray matches
    root = get_menu_root(page)

    # 1) Prefer quick ARIA-role attempts
    if _click_none_by_role(root.page):
        return SubscribeResult(True)

    # 2) CSS role scan inside root (this is likely what worked for you)
    if _click_none_by_css_scan(root.page):
        return SubscribeResult(True)

    # 3) Text-based selectors inside root
    for sel in NONE_SELECTORS["text_css"]:
        try:
            item = root.locator(sel).first
            item.wait_for(state="visible", timeout=1200)
            _scroll_into_view(item)
            item.click()
            page.wait_for_timeout(200)
            return SubscribeResult(True)
        except Exception:
            continue

    # 4) Absolute XPath fallback
    try:
        x = root.locator(NONE_SELECTORS["xpath_fallback"])
        if x.is_visible():
            _scroll_into_view(x)
            x.click()
            page.wait_for_timeout(200)
            return SubscribeResult(True)
    except Exception:
        pass

    return SubscribeResult(False, "notif 'None' option not found")

def subscribe_once(page: Page, channel_title: str, channel_url: str) -> SubscribeResult:
    """Go to channel, subscribe if needed, then ALWAYS set notifications to None."""
    try:
        page.goto(channel_url, wait_until="domcontentloaded")
        click_consent_if_present(page)
    except Exception as e:
        return SubscribeResult(False, f"navigation: {e}")

    try:
        # Try to subscribe (if not already)
        subscribe_btn = None
        for sel in SUBSCRIBE_SELECTORS:
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=int(CFG.wait_secs * 1000))
                subscribe_btn = loc
                break
            except Exception:
                continue

        if subscribe_btn:
            _scroll_into_view(subscribe_btn)
            try:
                subscribe_btn.click()
            except Exception:
                subscribe_btn.click(force=True)
            page.wait_for_timeout(500)
        else:
            # Ensure "Subscribed" is present
            ok = False
            for sel in SUBSCRIBED_SELECTORS:
                loc = page.locator(sel).first
                try:
                    loc.wait_for(state="visible", timeout=int(CFG.wait_secs * 1000))
                    ok = True
                    break
                except Exception:
                    continue
            if not ok:
                return SubscribeResult(False, "subscribe button not found")

        # Always set notifications to None
        notif = set_notifications_none(page)
        if not notif.ok:
            return SubscribeResult(True, f"subscribed but {notif.reason}")
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

        browser = p.chromium.launch(
            headless=CFG.headless_work,
            args=list(CFG.chromium_args),
            devtools=CFG.devtools,
            slow_mo=CFG.slowmo_ms or None,
        )
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
                    msg = f"Subscribed to {channel_title}"
                    if result.reason:
                        msg += f" — {result.reason}"
                    logging.info(msg)
                    log_fp.write(f"Subscribed: {channel_title} ({channel_url}){(' [' + result.reason + ']') if result.reason else ''}\n")
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
                    browser = p.chromium.launch(
                        headless=CFG.headless_work,
                        args=list(CFG.chromium_args),
                        devtools=CFG.devtools,
                        slow_mo=CFG.slowmo_ms or None,
                    )
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
# CLI
# ---------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Subscribe to channels and set notifications to None.")
    ap.add_argument("--headful", action="store_true", help="Run headful (visible) worker browser.")
    ap.add_argument("--slowmo", dest="slowmo_ms", type=int, default=0, help="Slow each action by N ms (e.g. 250).")
    ap.add_argument("--devtools", action="store_true", help="Open DevTools on the worker browser.")
    ap.add_argument("--none-labels", type=str, default="None,Κανένα",
                    help="Comma-separated labels for the 'None' option (localize if needed).")
    ap.add_argument("--debug", action="store_true", help="Enable DEBUG logging.")
    return ap.parse_args()

# ---------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------

def main() -> None:
    global CFG, NONE_SELECTORS

    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    labels = [x.strip() for x in (args.none_labels or "").split(",") if x.strip()]
    if not labels:
        labels = ["None"]
    NONE_SELECTORS = build_none_selectors(labels)

    CFG = replace(CFG,
                  headless_work=not args.headful,
                  slowmo_ms=args.slowmo_ms,
                  devtools=args.devtools)

    if not CFG.csv_file.exists():
        raise SystemExit(f"CSV file not found: {CFG.csv_file}")

    with sync_playwright() as p:
        state_file = ensure_auth_and_get_state_file(p, headless_for_check=True)
        run_worker_with_state(p, state_file)

if __name__ == "__main__":
    main()
