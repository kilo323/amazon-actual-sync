import logging

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersError
from amazonorders.contrib.browser.playwright import PlaywrightJSAuthForm

logger = logging.getLogger(__name__)


class FixedPlaywrightJSAuthForm(PlaywrightJSAuthForm):
    """
    Fixes PlaywrightJSAuthForm timing out when the JS challenge is served at the
    same URL as the resolved page (e.g. https://www.amazon.com/).

    The base class uses wait_for_url(), which registers a listener for *future*
    navigation events. When the challenge resolves in-place (no URL change),
    that event never fires and the 30-second timeout always triggers — even
    though the headless browser successfully passed the challenge.

    This override replaces wait_for_url() with page.goto(..., wait_until="load"),
    which already blocks until the page is fully loaded before returning.
    """

    def submit(self, last_response):
        if not self.amazon_session:
            raise AmazonOrdersError("Call select_form() first.")

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError as e:
            raise AmazonOrdersError(
                f"{type(self).__name__} requires the [browser] extra. "
                "Install it with: `pip install amazon-orders[browser]`, "
                "then: `playwright install chromium`"
            ) from e

        debug = self.amazon_session.debug
        if debug:
            logger.setLevel(logging.DEBUG)

        message = "Info: A browser is handling a JavaScript authentication challenge."
        logger.info(message)
        self.amazon_session.io.echo(message)
        output_dir = self.config.output_dir if debug else None
        original_url = last_response.url

        browser_name = self.config.browser or "chromium"
        with sync_playwright() as pw:
            browser_launcher = getattr(pw, browser_name, None)
            if browser_launcher is None:
                raise AmazonOrdersError(
                    f"Unsupported browser: {browser_name!r}. "
                    "Valid values: firefox, chromium."
                )
            browser = browser_launcher.launch(headless=self.headless)
            context = browser.new_context()
            self._inject_cookies(context, original_url)
            page = context.new_page()

            try:
                # goto() waits for the "load" event by default. By the time it
                # returns, Amazon has already served the real page — no further
                # navigation will occur and no wait_for_url is needed.
                page.goto(original_url, wait_until="load", timeout=30000)
            except PlaywrightTimeoutError as e:
                self._save_debug_snapshot(page, output_dir, "browser-timeout")
                browser.close()
                raise AmazonOrdersError(
                    "Browser timed out waiting for the JavaScript challenge to resolve."
                ) from e

            logger.debug("Browser challenge resolved, final URL: %s", page.url)
            self._save_debug_snapshot(page, output_dir, "browser-challenge")
            self._on_challenge_page(page, context, output_dir)

            final_url = page.url
            self._save_debug_snapshot(page, output_dir, "browser-resolved")
            self._harvest_cookies(context)
            browser.close()

        response = self.amazon_session.get(final_url, persist_cookies=True)
        self.clear_form()
        return response
