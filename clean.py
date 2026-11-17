import logging
import sys
import json

from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from config import CHATGPT_URL, HEADLESS, CHANNEL, USER_AGENT
from auth import get_accounts, get_session_path


CHAT_SELECTOR = 'a[href^="/c/"]'

SESSION_COOKIE = "__Secure-next-auth.session-token"


EXPIRY_WARNING_DAYS = 10

def check_token_expiry(context, account):
    """Fail if the session token is missing or expires within EXPIRY_WARNING_DAYS."""
    tokens = [c for c in context.cookies() if c["name"].startswith(SESSION_COOKIE)]
    if not tokens:
        raise RuntimeError(f"[{account}] No session token found. Run login.py {account}.")

    expiries = [c["expires"] for c in tokens if c["expires"] != -1]
    if not expiries:
        return                                  # browser-session cookie, no fixed expiry

    soonest = datetime.fromtimestamp(min(expiries), tz=timezone.utc)
    days_left = (soonest - datetime.now(timezone.utc)).days

    if days_left < EXPIRY_WARNING_DAYS:
        raise RuntimeError(
            f"[{account}] Session expires in {days_left} days ({soonest:%Y-%m-%d}). "
            f"Run login.py {account} and update the secret."
        )
    logger.info(f"[{account}] Session OK, {days_left} days left.")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)

def log_token_expiry(context, account, label):
    for c in context.cookies():
        if c["name"].startswith(SESSION_COOKIE):
            if c["expires"] == -1:
                logger.info(f"[{account}] {label}: {c['name']} has no expiry (browser-session cookie)")
            else:
                exp = datetime.fromtimestamp(c["expires"], tz=timezone.utc)
                logger.info(f"[{account}] {label}: {c['name']} expires {exp:%Y-%m-%d %H:%M} UTC")


def validate_session(page, account):
    logger.info(f"Checking login status for {account}")
    try:
        page.locator(CHAT_SELECTOR).first.wait_for(timeout=15000)
    except Exception:
        raise RuntimeError(f"Session is logged out for {account}.")
    logger.info(f"Session is logged in for {account}")

def get_chats(page):
    return page.locator(CHAT_SELECTOR)

def get_row(page, chat):
    """The chat's row (role=group, named by its title), narrowed to the one with this exact link."""
    title = chat.inner_text().strip()
    href = chat.get_attribute("href")
    row = page.get_by_role("group", name=title).filter(
        has=page.locator(f'a[href="{href}"]')
    )
    return row, title, href


def is_pinned(row):
    return row.get_by_role("button", name="Unpin chat", exact=True).count() > 0


def delete_chat(page, row, href):
    row.hover()
    row.get_by_label("Chat actions").click(timeout=5000)

    page.get_by_role("menuitem", name="Delete").click(timeout=5000)

    page.get_by_role("button", name="Delete chat", exact=True).click(timeout=5000)

    page.locator(f'a[href="{href}"]').wait_for(state="detached")

def load_more_chats(page):
    """Scroll to the bottom of the sidebar. Return True if more chats appeared."""
    chats = get_chats(page)
    before = chats.count()
    if before == 0:
        return False

    last = chats.last
    last.scroll_into_view_if_needed()
    last.hover()                      # put the mouse over the sidebar...
    page.mouse.wheel(0, 3000)         # ...and scroll it down

    for _ in range(10):               # wait up to 5 seconds for the next batch
        page.wait_for_timeout(500)
        if get_chats(page).count() > before:
            return True
    return False


def delete_all_unpinned(page):
    page.wait_for_timeout(3000)

    deleted_count = 0
    failed_hrefs = set()

    while True:
        chats = get_chats(page)
        count = chats.count()
        logger.info(f"Loaded chats: {count}")

        found_unpinned = False

        for i in range(count):
            chat = chats.nth(i)
            href = chat.get_attribute("href")

            if href in failed_hrefs:
                continue

            try:
                row, title, href = get_row(page, chat)

                pinned = is_pinned(row)
                logger.info(f"{title} | Pinned: {pinned}")
                if pinned:
                    continue

                delete_chat(page, row, href)
                deleted_count += 1
                found_unpinned = True
                logger.info(f"Deleted: {title}")
                break                       # DOM changed, rescan

            except Exception:
                failed_hrefs.add(href)
                logger.exception("Failed processing chat.")

        if not found_unpinned:
            if load_more_chats(page):
                logger.info("Loaded more chats from history.")
                continue                  # rescan with the new batch

            logger.info(f"Finished. Deleted {deleted_count}, failed {len(failed_hrefs)}.")
            if failed_hrefs:
                raise RuntimeError(f"{len(failed_hrefs)} chats could not be deleted")
            return deleted_count


def open_sidebar(page):
    chat_link = page.locator(CHAT_SELECTOR).first
    close_button = page.get_by_test_id("close-sidebar-button")
    open_button = page.get_by_role("button", name="Open sidebar")

    # Wait until the page shows either an open sidebar or the button to open it
    chat_link.or_(close_button).or_(open_button).first.wait_for()

    if chat_link.is_visible() or close_button.is_visible():
        logger.info("Sidebar already open.")
        return

    open_button.click()
    chat_link.wait_for()
    logger.info("Sidebar opened.")

def clean_account(browser, account):

    session_path = get_session_path(account)

    if not session_path.exists():
        logger.warning(
            f"Skipping {account}: session not found."
        )
        return False

    context = None

    try:
        context = browser.new_context(
            storage_state=session_path,
            user_agent=USER_AGENT,
            viewport={
                "width": 1280,
                "height": 720
            }
        )

        log_token_expiry(context, account, "before")
        page = context.new_page()

        page.goto(CHATGPT_URL)
        
        validate_session(page, account)

        logger.info(
            f"Cleaning account: {account}"
        )

        open_sidebar(page)
        delete_all_unpinned(page)
        log_token_expiry(context, account, "after")
        save_session(context, session_path)
        check_token_expiry(context, account)
        return True
    
    except Exception:
        logger.exception(
            f"Cleaner crashed for {account}."
        )
        return False

    finally:
        if context:
            context.close()

def save_session(context, session_path):
    state = context.storage_state()
    state["cookies"] = [c for c in state["cookies"]
                        if not c["name"].startswith(("cf_", "__cf", "_cf"))]
    session_path.write_text(json.dumps(state))


def main():
    failed = []
    with sync_playwright() as p:

        browser = p.chromium.launch(
            channel=CHANNEL,
            headless=HEADLESS
        )

        try:
            for account in get_accounts():
                if not clean_account(
                    browser,
                    account
                ):
                    failed.append(account)
        finally:
            browser.close()

        if failed:
            logger.error(f"Failed accounts: {', '.join(failed)}")
            sys.exit(1)

if __name__ == "__main__":
    main()