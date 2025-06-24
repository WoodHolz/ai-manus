from typing import Dict, Any, Optional, List
from playwright.async_api import async_playwright, Browser, Page
import asyncio
from markdownify import markdownify
from app.infrastructure.external.llm.openai_llm import OpenAILLM
from app.infrastructure.config import get_settings
from app.domain.models.tool_result import ToolResult
import logging

# Set up logger for this module
logger = logging.getLogger(__name__)

class PlaywrightBrowser:
    """Playwright client that provides specific implementation of browser operations"""
    
    def __init__(self, cdp_url: str, trace_events_queue: asyncio.Queue, proxy: Optional[Dict[str, str]] = None):
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.llm = OpenAILLM()
        self.settings = get_settings()
        self.cdp_url = cdp_url
        self.proxy = proxy
        self._trace_events_queue = trace_events_queue
        self._cdp_session = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop, creating a new one.")
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        
    @classmethod
    async def create(cls, cdp_url: str, trace_events_queue: asyncio.Queue, proxy: Optional[Dict[str, str]] = None):
        instance = cls(cdp_url, trace_events_queue, proxy)
        await instance.initialize()
        return instance

    async def initialize(self):
        """Initialize and ensure resources are available"""
        # Add retry logic
        max_retries = 5
        retry_delay = 1  # Initial wait 1 second
        for attempt in range(max_retries):
            try:
                self.playwright = await async_playwright().start()
                # Connect to existing Chrome instance
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
                # Get all contexts
                contexts = self.browser.contexts
                if contexts and len(contexts[0].pages) == 1:
                    # Check if it's the initial page (by URL)
                    page = contexts[0].pages[0]
                    page_url = await page.evaluate("window.location.href")
                    if (
                        page_url == "about:blank" or 
                        page_url == "chrome://newtab/" or 
                        page_url == "chrome://new-tab-page/" or 
                        not page_url
                    ):
                        # Only use it when it's the initial page and only one tab
                        self.page = page
                    else:
                        # Not the initial page, create a new page
                        self.page = await contexts[0].new_page()
                else:
                    # Create a new page in other cases
                    context = contexts[0] if contexts else await self.browser.new_context()
                    self.page = await context.new_page()
                
                # Start tracing: connect to CDP
                await self.start_tracing()
                
                return True
            except Exception as e:
                # Clean up failed resources
                await self.cleanup()
                
                # Return error if maximum retry count is reached
                if attempt == max_retries - 1:
                    logger.error(f"Initialization failed (retried {max_retries} times): {e}")
                    return False
                
                # Otherwise increase waiting time (exponential backoff strategy)
                retry_delay = min(retry_delay * 2, 10)  # Maximum wait 10 seconds
                logger.warning(f"Initialization failed, will retry in {retry_delay} seconds: {e}")
                await asyncio.sleep(retry_delay)

    async def cleanup(self):
        """Clean up Playwright resources, first close all tabs, then close the browser"""
        try:
            # If browser exists, first close all tabs
            if self.browser:
                # Get all contexts
                contexts = self.browser.contexts
                if contexts:
                    for context in contexts:
                        # Get all pages in the context
                        pages = context.pages
                        # Close all pages
                        for page in pages:
                            # Avoid closing self.page multiple times
                            if page != self.page or (self.page and not self.page.is_closed()):
                                await page.close()
            
            # Ensure the current page is closed (if it exists and is not closed)
            if self.page and not self.page.is_closed():
                await self.page.close()
                
            # Close the browser
            if self.browser:
                await self.browser.close()
                
            # Stop playwright
            if self.playwright:
                await self.playwright.stop()
                
        except Exception as e:
            logger.error(f"Error occurred when cleaning up resources: {e}")
        finally:
            # Reset references
            self.page = None
            self.browser = None
            self.playwright = None
            if self._cdp_session:
                try:
                    await self._cdp_session.detach()
                except Exception as e:
                    logger.warning(f"Failed to detach CDP session during cleanup: {e}")
                self._cdp_session = None
    
    async def start_tracing(self) -> None:
        """Start tracing by listening to high-level Playwright events."""
        if not self.page:
            logger.error("Cannot start tracing without a page.")
            return

        # Define a generic handler
        def create_handler(event_name):
            def handler(payload):
                try:
                    # For simplicity, we'll just log the event name.
                    # In a real scenario, you might want to serialize the payload.
                    event_data = {"type": "playwright_event", "event": event_name, "url": self.page.url}
                    asyncio.run_coroutine_threadsafe(self._trace_events_queue.put(event_data), self._loop)
                    print(f"Playwright event captured and queued: {event_name} on {self.page.url}")
                except Exception as e:
                    print(f"Error handling playwright event {event_name}: {e}")
            return handler

        # List of events to listen to
        events_to_listen = [
            "close", "crash", "domcontentloaded", "download", "filechooser",
            "frameattached", "framedetached", "framenavigated", "load", "pageerror",
            "popup", "request", "requestfailed", "requestfinished", "response", "websocket"
        ]
        
        # Clear existing listeners to avoid duplicates
        if hasattr(self, '_event_handlers'):
            for event, handler in self._event_handlers.items():
                try:
                    self.page.remove_listener(event, handler)
                except Exception as e:
                    logger.warning(f"Could not remove listener for {event}: {e}")

        # Attach new listeners
        self._event_handlers = {}
        for event_name in events_to_listen:
            handler = create_handler(event_name)
            self._event_handlers[event_name] = handler
            self.page.on(event_name, handler)
        
        logger.info(f"Attached high-level Playwright event listeners to page: {self.page.url}")

    async def _ensure_browser(self):
        """Ensure the browser is started"""
        if not self.browser or not self.page:
            if not await self.initialize():
                raise Exception("Unable to initialize browser resources")
    
    async def _ensure_page(self):
        """Ensure the page is created and update to the current active tab (rightmost tab)"""
        await self._ensure_browser()
        
        # Determine the currently active (rightmost) page
        rightmost_page = None
        if self.browser and self.browser.contexts:
            pages = self.browser.contexts[0].pages
            if pages:
                rightmost_page = pages[-1]
        
        # If we have a rightmost page and it's different from our current tracked page,
        # update our tracked page and re-initialize the tracer on it.
        if rightmost_page and self.page != rightmost_page:
            old_url = self.page.url if self.page else "N/A"
            new_url = rightmost_page.url
            logger.info(f"Switching active page. Old: {old_url}, New: {new_url}")
            self.page = rightmost_page
            # Re-start tracing on the new page to capture its events
            await self.start_tracing()
    
    async def wait_for_page_load(self, timeout: int = 15) -> bool:
        """Wait for the page to finish loading, waiting up to the specified timeout
        
        Args:
            timeout: Maximum wait time (seconds), default is 15 seconds
            
        Returns:
            bool: Whether successfully waited for the page to load completely
        """
        await self._ensure_page()
        
        start_time = asyncio.get_event_loop().time()
        check_interval = 5  # Check every 5 seconds
        
        while asyncio.get_event_loop().time() - start_time < timeout:
            # Check if the page has completely loaded
            is_loaded = await self.page.evaluate("""() => {
                return document.readyState === 'complete';
            }""")
            
            if is_loaded:
                return True
                
            # Wait for a while before checking again
            await asyncio.sleep(check_interval)
        
        # Timeout, page loading not completed
        return False
    
    async def _extract_content(self) -> Dict[str, Any]:
        """Extract content from the current page"""

        # Execute JavaScript to get elements in the viewport    
        visible_content = await self.page.evaluate("""() => {
            const visibleElements = [];
            const viewportHeight = window.innerHeight;
            const viewportWidth = window.innerWidth;
            
            // Get all potentially relevant elements
            const elements = document.querySelectorAll('body *');
            
            for (const element of elements) {
                // Check if the element is in the viewport and visible
                const rect = element.getBoundingClientRect();
                
                // Element must have some dimensions
                if (rect.width === 0 || rect.height === 0) continue;
                
                // Element must be within the viewport
                if (
                    rect.bottom < 0 || 
                    rect.top > viewportHeight ||
                    rect.right < 0 || 
                    rect.left > viewportWidth
                ) continue;
                
                // Check if the element is visible (not hidden by CSS)
                const style = window.getComputedStyle(element);
                if (
                    style.display === 'none' || 
                    style.visibility === 'hidden' || 
                    style.opacity === '0'
                ) continue;
                
                // If it's a text node or meaningful element, add it to the results
                if (
                    element.innerText || 
                    element.tagName === 'IMG' || 
                    element.tagName === 'INPUT' || 
                    element.tagName === 'BUTTON'
                ) {
                    visibleElements.push(element.outerHTML);
                }
            }
            
            // Build HTML containing these visible elements
            return '<div>' + visibleElements.join('') + '</div>';
        }""")

        
        # Convert to Markdown
        markdown_content = markdownify(visible_content)

        max_content_length = min(50000, len(markdown_content))
        response = await self.llm.ask([{
            "role": "system",
            "content": "You are a professional web page information extraction assistant. Please extract all information from the current page content and convert it to Markdown format."
        },
        {
            "role": "user",
            "content": markdown_content[:max_content_length]
        }
        ])
        
        return response.get("content", "")
    
    async def view_page(self) -> ToolResult:
        """View visible elements within the current page's viewport and convert to Markdown format"""
        await self._ensure_page()
        
        # Wait for the page to load completely, maximum wait 15 seconds
        await self.wait_for_page_load()
        
        # First update the interactive elements cache
        interactive_elements = await self._extract_interactive_elements()
        
        return ToolResult(
            success=True,
            data={
                "interactive_elements": interactive_elements,
                "content": await self._extract_content(),
            }
        )
    
    async def _extract_interactive_elements(self) -> List[str]:
        """Return a list of visible interactive elements on the page, formatted as index:<tag>text</tag>"""
        await self._ensure_page()
        
        # Clear the current page's cache to ensure we always get the latest list of elements
        self.page.interactive_elements_cache = []
        
        # Execute JavaScript to get interactive elements in the viewport
        interactive_elements = await self.page.evaluate("""() => {
            const interactiveElements = [];
            const viewportHeight = window.innerHeight;
            const viewportWidth = window.innerWidth;
            
            // Get all potentially relevant interactive elements
            const elements = document.querySelectorAll('button, a, input, textarea, select, [role="button"], [tabindex]:not([tabindex="-1"])');
            
            let validElementIndex = 0; // For generating consecutive indices
            
            for (let i = 0; i < elements.length; i++) {
                const element = elements[i];
                // Check if the element is in the viewport and visible
                const rect = element.getBoundingClientRect();
                
                // Element must have some dimensions
                if (rect.width === 0 || rect.height === 0) continue;
                
                // Element must be within the viewport
                if (
                    rect.bottom < 0 || 
                    rect.top > viewportHeight ||
                    rect.right < 0 || 
                    rect.left > viewportWidth
                ) continue;
                
                // Check if the element is visible (not hidden by CSS)
                const style = window.getComputedStyle(element);
                if (
                    style.display === 'none' || 
                    style.visibility === 'hidden' || 
                    style.opacity === '0'
                ) continue;
                
                
                // Get element type and text
                let tagName = element.tagName.toLowerCase();
                let text = '';
                
                if (element.value && ['input', 'textarea', 'select'].includes(tagName)) {
                    text = element.value;
                    
                    // Add label and placeholder information for input elements
                    if (tagName === 'input') {
                        // Get associated label text
                        let labelText = '';
                        if (element.id) {
                            const label = document.querySelector(`label[for="${element.id}"]`);
                            if (label) {
                                labelText = label.innerText.trim();
                            }
                        }
                        
                        // Look for parent or sibling label
                        if (!labelText) {
                            const parentLabel = element.closest('label');
                            if (parentLabel) {
                                labelText = parentLabel.innerText.trim().replace(element.value, '').trim();
                            }
                        }
                        
                        // Add label information
                        if (labelText) {
                            text = `[Label: ${labelText}] ${text}`;
                        }
                        
                        // Add placeholder information
                        if (element.placeholder) {
                            text = `${text} [Placeholder: ${element.placeholder}]`;
                        }
                    }
                } else if (element.innerText) {
                    text = element.innerText.trim().replace(/\\s+/g, ' ');
                } else if (element.alt) { // For image buttons
                    text = element.alt;
                } else if (element.title) { // For elements with title
                    text = element.title;
                } else if (element.placeholder) { // For placeholder text
                    text = `[Placeholder: ${element.placeholder}]`;
                } else if (element.type) { // For input type
                    text = `[${element.type}]`;
                    
                    // Add label and placeholder information for text-less input elements
                    if (tagName === 'input') {
                        // Get associated label text
                        let labelText = '';
                        if (element.id) {
                            const label = document.querySelector(`label[for="${element.id}"]`);
                            if (label) {
                                labelText = label.innerText.trim();
                            }
                        }
                        
                        // Look for parent or sibling label
                        if (!labelText) {
                            const parentLabel = element.closest('label');
                            if (parentLabel) {
                                labelText = parentLabel.innerText.trim();
                            }
                        }
                        
                        // Add label information
                        if (labelText) {
                            text = `[Label: ${labelText}] ${text}`;
                        }
                        
                        // Add placeholder information
                        if (element.placeholder) {
                            text = `${text} [Placeholder: ${element.placeholder}]`;
                        }
                    }
                } else {
                    text = '[No text]';
                }
                
                // Maximum limit on text length to keep it clear
                if (text.length > 100) {
                    text = text.substring(0, 97) + '...';
                }
                
                // Only add data-manus-id attribute to elements that meet the conditions
                element.setAttribute('data-manus-id', `manus-element-${validElementIndex}`);
                                                        
                // Build selector - using only data-manus-id
                const selector = `[data-manus-id="manus-element-${validElementIndex}"]`;
                
                // Add element information to the array
                interactiveElements.push({
                    index: validElementIndex,  // Use consecutive index
                    tag: tagName,
                    text: text,
                    selector: selector
                });
                
                validElementIndex++; // Increment valid element counter
            }
            
            return interactiveElements;
        }""")
        
        # Update cache
        self.page.interactive_elements_cache = interactive_elements
        
        # Format element information in specified format
        formatted_elements = []
        for el in interactive_elements:
            formatted_elements.append(f"{el['index']}:<{el['tag']}>{el['text']}</{el['tag']}>")
        
        return formatted_elements
    
    async def navigate(self, url: str, timeout: Optional[int] = 15000) -> ToolResult:
        """Navigate to the specified URL
        
        Args:
            url: URL to navigate to
            timeout: Navigation timeout (milliseconds), default is 60 seconds
        Returns:
            ToolResult: Result of navigation operation
        """
        await self._ensure_page()
        
        try:
            await self.page.goto(url, timeout=timeout)
            # After a successful navigation, we MUST re-attach the tracer to the new page context
            await self.start_tracing()
            logger.info(f"Successfully navigated to {url} and re-attached tracer.")
            return ToolResult(
                success=True,
                message=f"Successfully navigated to {url}"
            )
        except Exception as e:
            logger.error(f"Failed to navigate to {url}: {e}", exc_info=True)
            return ToolResult(
                success=False,
                message=f"Failed to navigate to {url}: {e}"
            )
    
    async def restart(self, url: str) -> ToolResult:
        """Restart the browser and navigate to the specified URL"""
        await self.cleanup()
        return await self.navigate(url)

    
    async def _get_element_by_index(self, index: int) -> Optional[Any]:
        """Get element by index using data-manus-id selector
        
        Args:
            index: Element index
            
        Returns:
            The found element, or None if not found
        """
        # Check if there are cached elements
        if not hasattr(self.page, 'interactive_elements_cache') or not self.page.interactive_elements_cache or index >= len(self.page.interactive_elements_cache):
            return None
        
        # Use data-manus-id selector
        selector = f'[data-manus-id="manus-element-{index}"]'
        return await self.page.query_selector(selector)
    
    async def click(
        self,
        index: Optional[int] = None,
        coordinate_x: Optional[float] = None,
        coordinate_y: Optional[float] = None
    ) -> ToolResult:
        """Click an element"""
        await self._ensure_page()
        if coordinate_x is not None and coordinate_y is not None:
            await self.page.mouse.click(coordinate_x, coordinate_y)
        elif index is not None:
            try:
                element = await self._get_element_by_index(index)
                if not element:
                    return ToolResult(success=False, message=f"Cannot find interactive element with index {index}")
                
                # Check if the element is visible
                is_visible = await self.page.evaluate("""(element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return !(
                        rect.width === 0 || 
                        rect.height === 0 || 
                        style.display === 'none' || 
                        style.visibility === 'hidden' || 
                        style.opacity === '0'
                    );
                }""", element)
                
                if not is_visible:
                    # Try to scroll to the element position
                    await self.page.evaluate("""(element) => {
                        if (element) {
                            element.scrollIntoView({behavior: 'smooth', block: 'center'});
                        }
                    }""", element)
                    # Wait for the element to become visible
                    await asyncio.sleep(1)
                
                # Try to click the element
                await element.click(timeout=5000)
            except Exception as e:
                return ToolResult(success=False, message=f"Failed to click element: {str(e)}")
        return ToolResult(success=True)
    
    async def input(
        self,
        text: str,
        press_enter: bool,
        index: Optional[int] = None,
        coordinate_x: Optional[float] = None,
        coordinate_y: Optional[float] = None
    ) -> ToolResult:
        """Input text"""
        await self._ensure_page()
        if coordinate_x is not None and coordinate_y is not None:
            await self.page.mouse.click(coordinate_x, coordinate_y)
            await self.page.keyboard.type(text)
        elif index is not None:
            try:
                element = await self._get_element_by_index(index)
                if not element:
                    return ToolResult(success=False, message=f"Cannot find interactive element with index {index}")
                
                # Try to use fill() method, but catch possible errors
                try:
                    await element.fill("")
                    await element.type(text)
                except Exception as e:
                    # If fill() fails, use type() method directly
                    await element.click()
                    await self.page.keyboard.type(text)
            except Exception as e:
                return ToolResult(success=False, message=f"Failed to input text: {str(e)}")
        
        if press_enter:
            await self.page.keyboard.press("Enter")
        return ToolResult(success=True)
    
    async def move_mouse(
        self,
        coordinate_x: float,
        coordinate_y: float
    ) -> ToolResult:
        """Move the mouse"""
        await self._ensure_page()
        await self.page.mouse.move(coordinate_x, coordinate_y)
        return ToolResult(success=True)
    
    async def press_key(self, key: str) -> ToolResult:
        """Simulate key press"""
        await self._ensure_page()
        await self.page.keyboard.press(key)
        return ToolResult(success=True)
    
    async def select_option(
        self,
        index: int,
        option: int
    ) -> ToolResult:
        """Select dropdown option"""
        await self._ensure_page()
        try:
            element = await self._get_element_by_index(index)
            if not element:
                return ToolResult(success=False, message=f"Cannot find selector element with index {index}")
            
            # Try to select the option
            await element.select_option(index=option)
            return ToolResult(success=True)
        except Exception as e:
            return ToolResult(success=False, message=f"Failed to select option: {str(e)}")
    
    async def scroll_up(
        self,
        to_top: Optional[bool] = None
    ) -> ToolResult:
        """Scroll up"""
        await self._ensure_page()
        if to_top:
            await self.page.evaluate("window.scrollTo(0, 0)")
        else:
            await self.page.evaluate("window.scrollBy(0, -window.innerHeight)")
        return ToolResult(success=True)
    
    async def scroll_down(
        self,
        to_bottom: Optional[bool] = None
    ) -> ToolResult:
        """Scroll down"""
        await self._ensure_page()
        if to_bottom:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
        return ToolResult(success=True)
    
    async def console_exec(self, javascript: str) -> ToolResult:
        """Execute JavaScript code"""
        await self._ensure_page()
        result = await self.page.evaluate(javascript)
        return ToolResult(success=True, data={"result": result})
    
    async def console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        """View console output"""
        await self._ensure_page()
        logs = await self.page.evaluate("""() => {
            return window.console.logs || [];
        }""")
        if max_lines is not None:
            logs = logs[-max_lines:]
        return ToolResult(success=True, data={"logs": logs})

    async def get_next_trace_event(self) -> Optional[Dict[str, Any]]:
        """Get the next trace event from the queue."""
        if self._trace_events_queue.empty():
            return None
        return await self._trace_events_queue.get()

    async def _on_event(self, event_type: str, event_payload):
        """Unified event handler for high-level Playwright events."""
        url = ""
        try:
            # Attempt to get the URL from the event payload, specific to event type
            if hasattr(event_payload, 'url'):
                url = event_payload.url
            elif hasattr(event_payload, 'request') and hasattr(event_payload.request, 'url'):
                url = event_payload.request.url
            elif hasattr(event_payload, 'frame'):
                url = event_payload.frame.url
        except Exception:
            # Some events might not have a URL or might be structured differently
            pass
        
        event_data = {
            "type": "playwright_event",
            "event": event_type,
            "url": url
        }
        
        # Using run_coroutine_threadsafe for thread safety from Playwright's sync context
        if self._loop:
            future = asyncio.run_coroutine_threadsafe(self._trace_events_queue.put(event_data), self._loop)
            try:
                future.result(timeout=5)  # Add a timeout to prevent indefinite blocking
            except Exception as e:
                logger.error(f"Error queuing Playwright event: {e}")

    async def _attach_page_listeners(self, page: Page):
        """Attach all high-level event listeners to the page."""
        event_types = [
            "close", "console", "crash", "dialog", "domcontentloaded", "download",
            "filechooser", "frameattached", "framedetached", "framenavigated",
            "load", "pageerror", "popup", "request", "requestfailed",
            "requestfinished", "response", "worker"
        ]
        for event_type in event_types:
            # Use a lambda to capture the current event_type
            page.on(event_type, lambda payload, et=event_type: self._on_event(et, payload))
        logger.info(f"Attached high-level Playwright event listeners to page: {page.url}")
