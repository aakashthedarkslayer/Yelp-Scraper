#!/usr/bin/env python3
"""
Yellow Pages AU - Electricians Scraper (Cloudflare Bypass)
Enhanced to bypass Cloudflare anti-bot detection
"""

import asyncio
import argparse
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PWTimeout,
)

if sys.version_info < (3, 9):
    sys.exit("Python 3.9+ required.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = "https://www.yellowpages.com.au"
LIST_URL       = "https://www.yellowpages.com.au/australia/electricians"
DEFAULT_DELAY  = 3.0  # Increased for Cloudflare
DEFAULT_MAX    = 3000
DEFAULT_OUTPUT = "electricians.xlsx"
RESULTS_PER_PAGE = 30
MAX_RETRIES = 3

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

# ── CSS selectors - UPDATED for current Yellow Pages structure ──────────────────
# These need to be verified with diagnose_selectors.py if page fails
SEL_CARD        = "div[class*='result']"     # Updated from div.result
SEL_NAME        = "a.business-name"
SEL_PHONE       = "div.phones.phone.primary"
SEL_STATUS      = "div.open-status"
SEL_WEBSITE_SRP = "a.yp-website-cta"
SEL_VERIFIED    = "div.years-with-yp"
SEL_WEBSITE_BPP = "a.yp-website-cta, a[class*='website'], a[data-event*='website']"
SEL_STATUS_BPP  = "div.open-status > span"
SEL_ADDRESS     = "div.adr"
SEL_NEXT_PAGE   = "a.next.ajax-page"

EMAIL_SKIP = {
    "yellowpages", "thryv", "noreply", "no-reply", "sentry", "cloudflare",
    "example", "ypcdn", "google", "facebook", "adobe", "doubleclick", "akamai", "cdn"
}


@dataclass
class Listing:
    name:       str = ""
    phone:      str = ""
    email:      str = ""
    status:     str = ""
    website:    str = ""
    verified:   str = ""
    address:    str = ""
    detail_url: str = ""


def jitter(base: float) -> float:
    """Add randomness to delay (±30%)"""
    return base * (0.7 + random.random() * 0.6)


async def qs_text(root, selector: str) -> str:
    """Query selector and get inner text"""
    try:
        el = await root.query_selector(selector)
        return (await el.inner_text()).strip() if el else ""
    except Exception:
        return ""


async def qs_attr(root, selector: str, attr: str) -> str:
    """Query selector and get attribute"""
    try:
        el = await root.query_selector(selector)
        val = await el.get_attribute(attr) if el else ""
        return (val or "").strip()
    except Exception:
        return ""


def clean_email(raw: str) -> str:
    """Extract and validate email from various formats"""
    email = raw.replace("mailto:", "").split("?")[0].strip().lower()
    
    if not email or "@" not in email:
        return ""
    
    parts = email.split("@")
    if len(parts) != 2 or "." not in parts[1]:
        return ""
    
    domain = parts[1]
    if any(skip in domain for skip in EMAIL_SKIP):
        return ""
    
    return email


def extract_emails_from_text(text: str) -> list[str]:
    """Extract all valid emails from text"""
    pattern = r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}"
    matches = re.findall(pattern, text.lower())
    
    valid = set()
    for m in matches:
        if clean_email(m):
            valid.add(m)
    
    return sorted(list(valid))


async def extract_email(page: Page, listing: Listing) -> str:
    """
    Extract email from Yellow Pages AU detail page.

    KEY DISCOVERY from real HTML analysis:
    - Cloudflare obfuscates the a.email-business href:
        href="/cdn-cgi/l/email-protection#<encoded>"  NOT mailto:
    - BUT the email appears plain text in TWO reliable places:
        1. Inline JS:  YPU = { "listing": { "email": "foo@bar.com" } }
        2. JSON-LD:    <script type="application/ld+json">{"email":"mailto:foo@bar.com"}</script>
    These are NEVER obfuscated and load with the initial HTML.
    """

    if not listing.detail_url:
        return ""

    found_emails: set[str] = set()

    # Strategy 0: YPU JavaScript object (fastest, most reliable)
    # Yellow Pages embeds all listing data in window.YPU.listing.email
    try:
        email_js = await page.evaluate("""
            () => {
                try {
                    if (window.YPU && window.YPU.listing && window.YPU.listing.email) {
                        return window.YPU.listing.email;
                    }
                } catch(e) {}
                return null;
            }
        """)
        if email_js:
            if email := clean_email(str(email_js)):
                log.debug("          [S0-YPU] %s", email)
                return email
    except Exception:
        pass

    # Strategy 1: JSON-LD schema tag (also server-rendered, never obfuscated)
    # <script type="application/ld+json">{"email":"mailto:foo@bar.com"}</script>
    try:
        import json as _json
        ld_scripts = await page.query_selector_all('script[type="application/ld+json"]')
        for ld in ld_scripts:
            try:
                raw = await ld.inner_text()
                if not raw or "@" not in raw:
                    continue
                data = _json.loads(raw)
                raw_email = data.get("email", "")
                if raw_email:
                    if email := clean_email(str(raw_email)):
                        log.debug("          [S1-JSONLD] %s", email)
                        return email
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 2: Regex on inline script text for "email":"value" pattern
    try:
        import re as _re
        scripts = await page.query_selector_all("script:not([src])")
        for script in scripts:
            try:
                text = await script.inner_text()
                if not text or "@" not in text:
                    continue
                for m in _re.finditer(r'"email"\s*:\s*"([^"]+)"', text):
                    candidate = m.group(1)
                    if email := clean_email(candidate):
                        found_emails.add(email)
                        log.debug("          [S2-script-regex] %s", email)
            except Exception:
                pass
    except Exception:
        pass

    if found_emails:
        return _best_email(found_emails)

    # Strategy 3: Cloudflare decoder — wait for CF email-decode.min.js to run
    # After it executes, href changes from /cdn-cgi/l/email-protection#... to mailto:...
    try:
        await asyncio.sleep(0.5)
        decoded_href = await page.evaluate("""
            () => {
                const el = document.querySelector('a.email-business');
                if (!el) return null;
                return el.getAttribute('href');
            }
        """)
        if decoded_href and decoded_href.startswith("mailto:"):
            if email := clean_email(decoded_href):
                log.debug("          [S3-CF-decoded] %s", email)
                return email
    except Exception:
        pass

    # Strategy 4: Full HTML scan (last resort)
    try:
        html = await page.content()
        if emails := extract_emails_from_text(html):
            found_emails.update(emails)
            log.debug("          [S4-html-scan] found: %s", emails)
    except Exception:
        pass

    return _best_email(found_emails) if found_emails else ""


def _best_email(emails: set[str]) -> str:
    """Pick the best email — prefer real business domains."""
    skip = {"yellowpages", "thryv", "noreply", "no-reply", "example", "test",
            "ypcdn", "cloudflare", "google", "facebook"}
    for email in sorted(emails):
        if not any(x in email for x in skip):
            return email
    return next(iter(emails), "")


async def parse_card(card) -> Listing:
    """Parse a single business card from list page"""
    listing = Listing()

    href = await qs_attr(card, SEL_NAME, "href")
    listing.name = await qs_text(card, SEL_NAME)
    if href:
        listing.detail_url = (BASE_URL + href) if href.startswith("/") else href

    listing.phone = await qs_text(card, SEL_PHONE)

    status_el = await card.query_selector(SEL_STATUS)
    if status_el:
        span = await status_el.query_selector("span")
        if span:
            listing.status = (await span.inner_text()).strip()
    listing.status = listing.status or "Unknown"

    listing.website = await qs_attr(card, SEL_WEBSITE_SRP, "href")

    v_el = await card.query_selector(SEL_VERIFIED)
    listing.verified = "Yes" if v_el else "No"

    listing.address = await qs_text(card, SEL_ADDRESS)

    return listing


async def scrape_detail(page: Page, listing: Listing, delay: float, retry_count: int = 0) -> None:
    """Scrape detail page for email and additional info"""
    if not listing.detail_url:
        return

    try:
        await page.goto(listing.detail_url, wait_until="domcontentloaded", timeout=40_000)

        # Wait for the business-info section to appear (where email lives)
        try:
            await page.wait_for_selector(
                "a.email-business, section#business-info, div.accordion",
                timeout=5_000,
            )
        except Exception:
            pass  # page may not have email — that's fine

        await asyncio.sleep(jitter(delay * 0.3))

        listing.email = await extract_email(page, listing)

        if not listing.website:
            try:
                site_el = await page.query_selector(SEL_WEBSITE_BPP)
                if site_el:
                    href = (await site_el.get_attribute("href") or "").strip()
                    if href and "yellowpages.com.au" not in href:
                        listing.website = href
            except Exception:
                pass

        if listing.status == "Unknown":
            try:
                span = await page.query_selector(SEL_STATUS_BPP)
                if span:
                    listing.status = (await span.inner_text()).strip() or "Unknown"
            except Exception:
                pass

    except PWTimeout:
        if retry_count < MAX_RETRIES:
            log.warning("  ⏱  Timeout (retry %d/%d)", retry_count + 1, MAX_RETRIES)
            await asyncio.sleep(jitter(delay * 2))
            await scrape_detail(page, listing, delay, retry_count + 1)
    except Exception as exc:
        if retry_count < MAX_RETRIES - 1:
            log.warning("  ⚠  Detail error (retry %d/%d): %s", retry_count + 1, MAX_RETRIES, type(exc).__name__)
            await asyncio.sleep(jitter(delay))
            await scrape_detail(page, listing, delay, retry_count + 1)


async def go_to_next_page(page: Page, current_page_num: int, delay: float) -> bool:
    """Navigate to next page by URL (pagination is a full reload, not AJAX)."""
    next_page_num = current_page_num + 1

    try:
        # Find the next page link — try several selectors to be robust
        next_link = await page.query_selector(f'a[data-page="{next_page_num}"]')
        if not next_link:
            for sel in ('.pagination a.next', 'a[rel="next"]', '.pagination a.next-page'):
                next_link = await page.query_selector(sel)
                if next_link:
                    break

        if not next_link:
            # Fallback: build the URL ourselves — YP supports ?page=N directly
            next_url = f"{LIST_URL}?page={next_page_num}"
            log.info("   ⏩ No next link found; trying direct URL: %s", next_url)
        else:
            href = await next_link.get_attribute("href")
            if not href:
                log.info("   ✓ Next link has no href — done.")
                return False
            next_url = (BASE_URL + href) if href.startswith("/") else href
            log.info("   ⏩ Navigating to page %d  →  %s", next_page_num, next_url)

        # Full navigation — pagination is a real page load, not AJAX
        await page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)

        # Cloudflare check on each navigation
        title = await page.title()
        if "cloudflare" in title.lower() or "attention" in title.lower():
            log.error("   ❌ Cloudflare block on page %d — waiting 60s", next_page_num)
            await asyncio.sleep(60)
            await page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)

        try:
            await page.wait_for_selector(SEL_CARD, timeout=20_000)
        except PWTimeout:
            log.warning("   ⚠  Cards never appeared on page %d", next_page_num)
            return False

        await asyncio.sleep(jitter(delay * 0.5))

        cards_after = await page.query_selector_all(SEL_CARD)
        if not cards_after:
            log.info("   ✓ No cards on page %d — done.", next_page_num)
            return False

        log.info("   ✅ Page %d loaded — %d cards", next_page_num, len(cards_after))
        return True

    except Exception as exc:
        log.warning("   ⚠  Pagination error: %s", exc)
        return False


async def new_context(browser, headless: bool = True) -> BrowserContext:
    """Create new browser context with anti-bot stealth measures"""
    return await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1280, "height": 900},
        locale="en-AU",
        timezone_id="Australia/Sydney",
        extra_http_headers={
            "Accept-Language": "en-AU,en;q=0.9",
            "Referer": BASE_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )


def save(records: list[Listing], path: str) -> None:
    """Save records to Excel file"""
    if not records:
        log.warning("No records to save yet.")
        return

    rows = [asdict(r) for r in records]
    df = pd.DataFrame(rows)
    
    df.rename(columns={
        "name":       "Name",
        "phone":      "Phone",
        "email":      "Email",
        "status":     "Status",
        "website":    "Website",
        "verified":   "Verified",
        "address":    "Address",
        "detail_url": "Detail URL",
    }, inplace=True)

    cols = ["Name", "Phone", "Email", "Status", "Website", "Verified", "Address", "Detail URL"]
    df = df[[c for c in cols if c in df.columns]]

    df.to_excel(path, index=False, engine="openpyxl")
    log.info("  💾 Saved %d records → %s", len(records), path)


async def scrape(max_records: int, delay: float, output: str, headless: bool = True) -> None:
    """Main scraping loop"""
    results: list[Listing] = []
    page_num = 1
    start_time = time.time()

    async with async_playwright() as pw:
        # Launch with minimal bot detection
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        ctx         = await new_context(browser, headless)
        list_page   = await ctx.new_page()
        detail_page = await ctx.new_page()

        # Stealth mode: hide automation signals
        await list_page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
        """)

        try:
            log.info("📄 Loading page 1  →  %s", LIST_URL)
            await list_page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30_000)
            
            # Check if we got Cloudflare blocked
            title = await list_page.title()
            if "cloudflare" in title.lower() or "attention" in title.lower():
                log.error("❌ CLOUDFLARE BLOCK DETECTED!")
                log.error("   Title: %s", title)
                log.error("   Waiting 60 seconds before retry...")
                await asyncio.sleep(60)
                await list_page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30_000)
            
            await asyncio.sleep(jitter(delay))

            while len(results) < max_records:
                cards = await list_page.query_selector_all(SEL_CARD)

                if not cards:
                    log.info("   ✓ No cards on page %d — stopping.", page_num)
                    break

                log.info("   📍 Page %d: %d listings found", page_num, len(cards))

                for idx, card in enumerate(cards, 1):
                    if len(results) >= max_records:
                        break

                    listing = await parse_card(card)
                    if not listing.name:
                        continue

                    current_num = len(results) + 1
                    log.info("   [%d/%d] %s  |  %s",
                             current_num, max_records,
                             listing.name[:40], listing.phone or "no phone")

                    if listing.detail_url:
                        await scrape_detail(detail_page, listing, delay)

                    if listing.email:
                        log.info("          ✉  %s", listing.email)
                    else:
                        log.info("          ✉  (not found)")

                    results.append(listing)

                    if len(results) % 50 == 0:
                        save(results, output)

                    await asyncio.sleep(jitter(delay))

                if len(results) >= max_records:
                    log.info("   🎯 Reached target of %d records.", max_records)
                    break

                moved = await go_to_next_page(list_page, page_num, delay)
                if not moved:
                    log.info("   ✓ No more pages — scraping complete.")
                    break

                page_num += 1

                if page_num % 10 == 0:
                    log.info("   🔄 Rotating browser context...")
                    current_url = list_page.url
                    await ctx.close()
                    ctx         = await new_context(browser, headless)
                    list_page   = await ctx.new_page()
                    detail_page = await ctx.new_page()
                    await list_page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                        });
                    """)
                    await list_page.goto(current_url, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(jitter(delay))

        finally:
            await browser.close()

    save(results, output)
    
    elapsed = time.time() - start_time
    emails_found = sum(1 for r in results if r.email)
    
    log.info("✅ Scraping Complete!")
    log.info("   Total Records : %d / %d", len(results), max_records)
    log.info("   Emails Found  : %d (%.1f%%)", emails_found, 
             (emails_found / len(results) * 100) if results else 0)
    log.info("   Time Elapsed  : %.1f minutes", elapsed / 60)
    log.info("   Output File   : %s", output)


def main() -> None:
    """Command-line interface"""
    parser = argparse.ArgumentParser(
        description="Yellow Pages AU Electricians Scraper (Cloudflare Bypass)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--max", type=int, default=DEFAULT_MAX,
                        help="Max records to collect (default: 3000)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help="Base delay between requests in seconds (default: 3.0)")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help="Output Excel file (default: electricians.xlsx)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Run with visible browser (debugging)")
    args = parser.parse_args()

    log.info("🔌 Yellow Pages AU — Electricians Scraper (Cloudflare Bypass)")
    log.info("   Target  : %s", LIST_URL)
    log.info("   Max     : %d records", args.max)
    log.info("   Delay   : %.1fs ± 30%%", args.delay)
    log.info("   Output  : %s", args.output)
    log.info("   Pages   : ~%d needed", -(-args.max // RESULTS_PER_PAGE))
    log.info("   Mode    : %s", "Headless" if args.headless else "Visible")

    asyncio.run(scrape(args.max, args.delay, args.output, args.headless))


if __name__ == "__main__":
    main()
