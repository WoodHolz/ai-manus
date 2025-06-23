import logging
from typing import List, Dict
from playwright.async_api import async_playwright
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from functools import lru_cache
import base64
from playwright._impl._errors import TimeoutError

# Configure logger
logger = logging.getLogger(__name__)

async def _click_robustly(page_or_frame, selector: str, description: str, timeout: int = 10000):
    """A helper function to try clicking an element, with a fallback to forceful clicking."""
    logger.info(f"Attempting to click: {description} ('{selector}')")
    try:
        await page_or_frame.locator(selector).click(timeout=timeout)
        logger.info(f"Successfully clicked '{selector}' (standard click).")
    except TimeoutError:
        logger.warning(f"Standard click failed for '{selector}'. Trying a forceful click.")
        await page_or_frame.locator(selector).click(timeout=timeout, force=True)
        logger.info(f"Successfully clicked '{selector}' (forceful click).")

class SessionService:
    async def create_and_run_session(self, url: str, cookies: list[dict]) -> dict:
        sandbox = None
        p = None
        result = {}
        debug_screenshot_base64 = None
        debug_html = None
        page = None

        try:
            # Step 1 & 2: Connect to browser and navigate
            logger.info("Step 1 & 2: Starting sandbox and navigating to page...")
            sandbox = await DockerSandbox.create()
            p = await async_playwright().start()
            browser = await p.chromium.connect_over_cdp(sandbox.cdp_url)
            context = browser.contexts[0]
            await context.add_cookies(cookies)
            page = context.pages[0]
            await page.goto(url, wait_until="networkidle")
            logger.info(f"Initial navigation to {url} complete.")

            # --- NEW: Add a fixed delay to allow client-side scripts to run ---
            logger.info("Waiting for 5 seconds to allow for popups or redirects to appear...")
            await page.wait_for_timeout(5000)
            
            # --- NEW: Pre-script handler for popups and redirects ---
            logger.info("Executing pre-script handler for popups and redirects...")
            try:
                # This is the ULTIMATE selector. It finds the button that is a sibling
                # to the specific dialog box containing our unique text.
                popup_button_selector = "div#quitTipsbox + div.dialog-button a:has-text('确定')"
                await _click_robustly(page, popup_button_selector, "Popup confirm button", timeout=5000)
                
                logger.info("Popup confirm button clicked successfully.")
                await page.wait_for_load_state("networkidle")
            except TimeoutError:
                logger.info("Popup confirm handler failed or timed out. Assuming no popup.")

            try:
                login_button_selector = "#login_main > div.login_form > div > div:nth-child(3) > div.form_btn"
                await _click_robustly(page, login_button_selector, "Login page button", timeout=5000)
                await page.wait_for_selector("#nav > li.active", timeout=15000)
            except TimeoutError:
                logger.info("Login page handler failed or timed out. Assuming already on main page.")
            
            # Take a screenshot right before executing the main script
            logger.info("Capturing pre-main-script screenshot...")
            screenshot_bytes = await page.screenshot(full_page=True)
            debug_screenshot_base64 = base64.b64encode(screenshot_bytes).decode()
            logger.info("Screenshot captured.")

            # Step 3: Execute the main automation script
            logger.info("Step 3: Executing main script for CNKI website...")
            
            script = [
                {"action": "click", "selector": "#nav > li.active", "description": "Clicking on the main active menu to expand"},
                {"action": "click", "selector": "#nav > li.active > ul > li:nth-child(6)", "description": "Clicking on the 6th sub-menu item (System Announcements)"},
                {"action": "wait_for_response", "url_pattern": "**/SystemSet.ashx?action=GetAIGCCheck", "description": "Waiting for the announcement data to load"},
                {"action": "extract_text", "selector": "#datagrid-row-r1-2-0 > td:nth-child(6) > div > span", "field_name": "first_announcement_status", "description": "Extracting status from the first row"},
            ]

            extracted_data = {}
            for step in script:
                action = step["action"]
                description = step.get("description", "No description")
                logger.info(f"Executing step: {description}")

                if action == "click":
                    await _click_robustly(page, step["selector"], description)
                elif action == "wait_for_response":
                    async with page.expect_response(step["url_pattern"]) as response_info:
                        logger.info(f"Waiting for response matching pattern: {step['url_pattern']}")
                    response = await response_info.value
                    logger.info(f"Received response from {response.url} with status {response.status}")
                elif action == "extract_text":
                    text = await page.locator(step["selector"]).inner_text()
                    field_name = step["field_name"]
                    extracted_data[field_name] = text
                    logger.info(f"Extraction successful: {{'{field_name}': '{text}'}}")
            
            logger.info("Script execution finished.")
            result = {
                "session_id": sandbox.id,
                "status": "completed",
                "extracted_data": extracted_data
            }

        except Exception as e:
            logger.error(f"An error occurred during session execution: {str(e)}")
            logger.error("Traceback:", exc_info=True)
            if page:
                logger.info("Capturing screenshot on failure...")
                screenshot_bytes = await page.screenshot(full_page=True)
                debug_screenshot_base64 = base64.b64encode(screenshot_bytes).decode()
                logger.info("Failure screenshot captured.")
                logger.info("Capturing HTML on failure...")
                debug_html = await page.content()
                logger.info("Failure HTML captured.")
            result = {"session_id": "dev-sandbox", "status": "failed", "error": str(e)}
        finally:
            result["debug_screenshot_base64"] = debug_screenshot_base64
            result["debug_html"] = debug_html
            # Step 4: Clean up resources
            logger.info("Step 4: Cleaning up resources...")
            if p:
                await p.stop()
                logger.info("Playwright stopped.")
            if sandbox:
                await sandbox.destroy()
                logger.info(f"Sandbox '{sandbox.id}' destroyed.")
        
        return result

@lru_cache(maxsize=1)
def get_session_service() -> SessionService:
    return SessionService() 