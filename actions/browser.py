"""Browser automation via Playwright — navigate, screenshot, extract, fill forms.

Every action goes through Guardian review. Financial sites are hard-blocked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from urllib.parse import urlparse

log = logging.getLogger("khalil.actions.browser")

# Hard guardrail: block browser actions on financial sites
FINANCIAL_DOMAINS = {
    "bank", "chase", "citibank", "td", "rbc", "bmo", "scotiabank",
    "paypal", "venmo", "wealthsimple", "questrade", "robinhood",
    "coinbase", "binance", "interac", "mint", "plaid",
    "americanexpress", "amex", "visa", "mastercard",
}


def is_financial_url(url: str) -> bool:
    """Check if a URL belongs to a financial institution. Hard guardrail."""
    try:
        domain = urlparse(url).hostname or ""
        domain_lower = domain.lower()
        # Check each financial keyword against domain parts
        for keyword in FINANCIAL_DOMAINS:
            if keyword in domain_lower:
                return True
        return False
    except Exception:
        return False


async def _get_browser():
    """Get or launch a headless Playwright browser instance."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && playwright install chromium"
        )

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return pw, browser


async def navigate_and_screenshot(url: str) -> str | None:
    """Navigate to URL, wait for load, take screenshot.

    Returns path to screenshot PNG or None on failure.
    """
    if is_financial_url(url):
        log.warning("Blocked browser access to financial URL: %s", url)
        return None

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        output_path = tempfile.mktemp(suffix=".png")
        await page.screenshot(path=output_path, full_page=False)

        await page.close()
        return output_path if os.path.exists(output_path) else None
    except Exception as e:
        log.error("Browser screenshot failed: %s", e)
        return None
    finally:
        await browser.close()
        await pw.stop()


async def extract_page_text(url: str, selector: str | None = None, max_chars: int = 5000) -> str:
    """Navigate to URL and extract visible text content.

    Args:
        url: Page URL to extract from.
        selector: Optional CSS selector to extract from specific element.
        max_chars: Maximum characters to return.
    """
    if is_financial_url(url):
        return "Blocked: financial site access not allowed."

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        if selector:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
            else:
                text = f"Selector '{selector}' not found on page."
        else:
            text = await page.inner_text("body")

        await page.close()
        return text[:max_chars]
    except Exception as e:
        log.error("Browser text extraction failed: %s", e)
        return f"Error: {e}"
    finally:
        await browser.close()
        await pw.stop()


async def click_element(url: str, selector: str) -> dict:
    """Navigate to URL and click an element.

    Returns {"success": bool, "error": str | None, "screenshot": str | None}
    """
    if is_financial_url(url):
        return {"success": False, "error": "Blocked: financial site"}

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        element = await page.query_selector(selector)
        if not element:
            await page.close()
            return {"success": False, "error": f"Element '{selector}' not found"}

        await element.click()
        await page.wait_for_load_state("networkidle", timeout=10000)

        # Take screenshot after click
        screenshot_path = tempfile.mktemp(suffix=".png")
        await page.screenshot(path=screenshot_path, full_page=False)

        await page.close()
        return {"success": True, "error": None, "screenshot": screenshot_path}
    except Exception as e:
        log.error("Browser click failed: %s", e)
        return {"success": False, "error": str(e)}
    finally:
        await browser.close()
        await pw.stop()


async def fill_form(url: str, fields: dict[str, str]) -> dict:
    """Navigate to URL and fill form fields.

    Args:
        url: Page URL with the form.
        fields: {css_selector: value} mapping of fields to fill.

    Returns {"success": bool, "error": str | None, "screenshot": str | None}
    """
    if is_financial_url(url):
        return {"success": False, "error": "Blocked: financial site"}

    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        for selector, value in fields.items():
            element = await page.query_selector(selector)
            if not element:
                await page.close()
                return {"success": False, "error": f"Field '{selector}' not found"}
            await element.fill(value)

        # Screenshot after filling
        screenshot_path = tempfile.mktemp(suffix=".png")
        await page.screenshot(path=screenshot_path, full_page=False)

        await page.close()
        return {"success": True, "error": None, "screenshot": screenshot_path}
    except Exception as e:
        log.error("Browser fill failed: %s", e)
        return {"success": False, "error": str(e)}
    finally:
        await browser.close()
        await pw.stop()
