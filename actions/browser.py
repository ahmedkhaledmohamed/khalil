"""Browser automation via Playwright — navigate, screenshot, extract, fill.

All navigation is headless Chromium. Financial domains are hard-blocked.
"""

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("khalil.actions.browser")

# Hard guardrail: never automate financial sites
FINANCIAL_DOMAINS = {
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citibank.com",
    "td.com", "tdcanadatrust.com", "rbc.com", "scotiabank.com", "bmo.com",
    "paypal.com", "venmo.com", "wealthsimple.com", "questrade.com",
    "interactivebrokers.com", "fidelity.com", "schwab.com", "vanguard.com",
}


def is_financial_url(url: str) -> bool:
    """Check if URL belongs to a financial institution."""
    try:
        domain = urlparse(url).netloc.lower()
        return any(fin in domain for fin in FINANCIAL_DOMAINS)
    except Exception:
        return True  # err on the side of caution


async def _get_browser():
    """Get or launch headless Chromium via Playwright."""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return pw, browser


async def navigate_and_screenshot(url: str) -> tuple[str | None, str]:
    """Navigate to URL and capture screenshot. Returns (screenshot_path, page_title)."""
    if is_financial_url(url):
        return None, "BLOCKED: Financial site — cannot automate"

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        title = await page.title()
        path = "/tmp/khalil_browser_screenshot.png"
        await page.screenshot(path=path, full_page=False)
        return path, title
    finally:
        await browser.close()
        await pw.stop()


async def extract_page_text(url: str, selector: str | None = None) -> str:
    """Extract text content from a page, optionally filtered by CSS selector."""
    if is_financial_url(url):
        return "BLOCKED: Financial site — cannot automate"

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        if selector:
            elements = await page.query_selector_all(selector)
            texts = []
            for el in elements:
                t = await el.text_content()
                if t:
                    texts.append(t.strip())
            return "\n".join(texts)
        else:
            return await page.inner_text("body")
    finally:
        await browser.close()
        await pw.stop()


async def click_element(url: str, selector: str) -> dict:
    """Navigate to URL and click an element. Returns status dict."""
    if is_financial_url(url):
        return {"error": "BLOCKED: Financial site"}

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        await page.click(selector, timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        return {"ok": True, "title": await page.title(), "url": page.url}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await browser.close()
        await pw.stop()


async def fill_form(url: str, fields: dict[str, str]) -> dict:
    """Navigate to URL and fill form fields (selector -> value)."""
    if is_financial_url(url):
        return {"error": "BLOCKED: Financial site"}

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        for selector, value in fields.items():
            await page.fill(selector, value, timeout=5000)
        return {"ok": True, "filled": len(fields)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await browser.close()
        await pw.stop()
