from __future__ import annotations

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def launch_browser(headed: bool = False) -> tuple[Playwright, Browser]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, browser


async def new_context(browser: Browser, locale: str = "en-GB") -> BrowserContext:
    return await browser.new_context(
        user_agent=USER_AGENT,
        locale=locale,
        viewport={"width": 1400, "height": 900},
    )


async def safe_goto(page: Page, url: str, timeout: int = 45_000, wait: str = "domcontentloaded") -> None:
    try:
        await page.goto(url, wait_until=wait, timeout=timeout)
    except Exception as exc:
        print(f"  Warning navigating {url}: {exc}")


async def dismiss_cookies(page: Page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('I Accept')",
        "button:has-text('Allow all')",
        "button:has-text('Accept')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=2000, force=True)
                await page.wait_for_timeout(400)
                break
        except Exception:
            continue
    try:
        await page.evaluate(
            """() => {
              for (const id of ['onetrust-consent-sdk', 'onetrust-banner-sdk', 'onetrust-pc-sdk']) {
                const el = document.getElementById(id);
                if (el) el.remove();
              }
            }"""
        )
    except Exception:
        pass
