from typing import Dict, Any, Optional, List
import uuid
import httpx
import docker
import socket
import logging
import asyncio
from async_lru import alru_cache
from app.infrastructure.config import get_settings
from app.domain.models.tool_result import ToolResult
from app.domain.external.sandbox import Sandbox
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
from app.domain.external.browser import Browser
from app.domain.external.llm import LLM
import os
import json

logger = logging.getLogger(__name__)

class DockerSandbox(Sandbox):
    def __init__(self, ip: str = None, container_name: str = None, session_id: str = None):
        """Initialize Docker sandbox and API interaction client"""
        self.client = httpx.AsyncClient(timeout=600)
        self.ip = ip
        self._container_name = container_name
        self.session_id = session_id
        self.logger = logging.getLogger(f"{__name__}.{self.id}")
        self.base_url = f"http://{self.ip}:8080"
        self._vnc_url = f"ws://{self.ip}:5901"
        self._cdp_url = f"http://{self.ip}:9222"
        self._browser: Optional[Browser] = None
        self._trace_events_queue = asyncio.Queue()
    
    @property
    def id(self) -> str:
        """Sandbox ID"""
        if not self._container_name:
            return "dev-sandbox"
        return self._container_name
    
    
    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def vnc_url(self) -> str:
        return self._vnc_url

    @staticmethod
    def _get_container_ip(container) -> str:
        """Get container IP address from network settings
        
        Args:
            container: Docker container instance
            
        Returns:
            Container IP address
        """
        # Get container network settings
        network_settings = container.attrs['NetworkSettings']
        ip_address = network_settings['IPAddress']
        
        # If default network has no IP, try to get IP from other networks
        if not ip_address and 'Networks' in network_settings:
            networks = network_settings['Networks']
            # Try to get IP from first available network
            for network_name, network_config in networks.items():
                if 'IPAddress' in network_config and network_config['IPAddress']:
                    ip_address = network_config['IPAddress']
                    break
        
        return ip_address

    @staticmethod
    def _create_task(session_id: str) -> 'DockerSandbox':
        """Create a new Docker sandbox (static method)
        
        Args:
            image: Docker image name
            name_prefix: Container name prefix
            
        Returns:
            DockerSandbox instance
        """
        # Use configured default values
        settings = get_settings()

        image = settings.sandbox_image
        name_prefix = settings.sandbox_name_prefix
        container_name = f"{name_prefix}-{str(uuid.uuid4())[:8]}"
        
        try:
            # Create Docker client
            docker_client = docker.from_env()

            # Prepare container configuration
            container_config = {
                "image": image,
                "name": container_name,
                "detach": True,
                "auto_remove": True,
                "environment": {
                    "SERVICE_TIMEOUT_MINUTES": settings.sandbox_ttl_minutes,
                    "CHROME_ARGS": settings.sandbox_chrome_args,
                    "HTTPS_PROXY": settings.sandbox_https_proxy,
                    "HTTP_PROXY": settings.sandbox_http_proxy,
                    "NO_PROXY": settings.sandbox_no_proxy
                }
            }

            # Add network to container config if configured
            if settings.sandbox_network:
                container_config["network"] = settings.sandbox_network
            
            # Create container
            container = docker_client.containers.run(**container_config)
            
            # Get container IP address
            container.reload()  # Refresh container info
            ip_address = DockerSandbox._get_container_ip(container)
            
            # Create and return DockerSandbox instance
            return DockerSandbox(
                ip=ip_address,
                container_name=container_name,
                session_id=session_id
            )
            
        except Exception as e:
            raise Exception(f"Failed to create Docker sandbox: {str(e)}")

    async def exec_command(self, session_id: str, exec_dir: str, command: str) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/exec",
            json={
                "id": session_id,
                "exec_dir": exec_dir,
                "command": command
            }
        )
        return ToolResult(**response.json())

    async def view_shell(self, session_id: str) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/view",
            json={"id": session_id}
        )
        return ToolResult(**response.json())

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/wait",
            json={
                "id": session_id,
                "seconds": seconds
            }
        )
        return ToolResult(**response.json())

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool = True) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/write",
            json={
                "id": session_id,
                "input": input_text,
                "press_enter": press_enter
            }
        )
        return ToolResult(**response.json())

    async def kill_process(self, session_id: str) -> ToolResult:
        response = await self.client.post(
            f"{self.base_url}/api/v1/shell/kill",
            json={"id": session_id}
        )
        return ToolResult(**response.json())

    async def file_write(self, file: str, content: str, append: bool = False, 
                        leading_newline: bool = False, trailing_newline: bool = False, 
                        sudo: bool = False) -> ToolResult:
        """Write content to file
        
        Args:
            file: File path
            content: Content to write
            append: Whether to append content
            leading_newline: Whether to add newline before content
            trailing_newline: Whether to add newline after content
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of write operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/write",
            json={
                "file": file,
                "content": content,
                "append": append,
                "leading_newline": leading_newline,
                "trailing_newline": trailing_newline,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_read(self, file: str, start_line: int = None, 
                        end_line: int = None, sudo: bool = False) -> ToolResult:
        """Read file content
        
        Args:
            file: File path
            start_line: Start line number
            end_line: End line number
            sudo: Whether to use sudo privileges
            
        Returns:
            File content
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/read",
            json={
                "file": file,
                "start_line": start_line,
                "end_line": end_line,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())
        
    async def file_exists(self, path: str) -> ToolResult:
        """Check if file exists
        
        Args:
            path: File path
            
        Returns:
            Whether file exists
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/exists",
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_delete(self, path: str) -> ToolResult:
        """Delete file
        
        Args:
            path: File path
            
        Returns:
            Result of delete operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/delete",
            json={"path": path}
        )
        return ToolResult(**response.json())
        
    async def file_list(self, path: str) -> ToolResult:
        """List directory contents
        
        Args:
            path: Directory path
            
        Returns:
            List of directory contents
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/list",
            json={"path": path}
        )
        return ToolResult(**response.json())

    async def file_replace(self, file: str, old_str: str, new_str: str, sudo: bool = False) -> ToolResult:
        """Replace string in file
        
        Args:
            file: File path
            old_str: String to replace
            new_str: String to replace with
            sudo: Whether to use sudo privileges
            
        Returns:
            Result of replace operation
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/replace",
            json={
                "file": file,
                "old_str": old_str,
                "new_str": new_str,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_search(self, file: str, regex: str, sudo: bool = False) -> ToolResult:
        """Search in file content
        
        Args:
            file: File path
            regex: Regular expression
            sudo: Whether to use sudo privileges
            
        Returns:
            Search results
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/search",
            json={
                "file": file,
                "regex": regex,
                "sudo": sudo
            }
        )
        return ToolResult(**response.json())

    async def file_find(self, path: str, glob_pattern: str) -> ToolResult:
        """Find files by name pattern
        
        Args:
            path: Search directory path
            glob_pattern: Glob match pattern
            
        Returns:
            List of found files
        """
        response = await self.client.post(
            f"{self.base_url}/api/v1/file/find",
            json={
                "path": path,
                "glob": glob_pattern
            }
        )
        return ToolResult(**response.json())
    
    @staticmethod
    @alru_cache(maxsize=128, typed=True)
    async def _resolve_hostname_to_ip(hostname: str) -> str:
        """Resolve hostname to IP address
        
        Args:
            hostname: Hostname to resolve
            
        Returns:
            Resolved IP address, or None if resolution fails
            
        Note:
            This method is cached using LRU cache with a maximum size of 128 entries.
            The cache helps reduce repeated DNS lookups for the same hostname.
        """
        try:
            # First check if hostname is already in IP address format
            try:
                socket.inet_pton(socket.AF_INET, hostname)
                # If successfully parsed, it's an IPv4 address format, return directly
                return hostname
            except OSError:
                # Not a valid IP address format, proceed with DNS resolution
                pass
                
            # Use socket.getaddrinfo for DNS resolution
            addr_info = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
            # Return the first IPv4 address found
            if addr_info and len(addr_info) > 0:
                return addr_info[0][4][0]  # Return sockaddr[0] from (family, type, proto, canonname, sockaddr), which is the IP address
            return None
        except Exception as e:
            # Log error and return None on failure
            logger.error(f"Failed to resolve hostname {hostname}: {str(e)}")
            return None
    
    async def destroy(self) -> bool:
        """Destroy Docker sandbox"""
        try:
            if self.client:
                await self.client.aclose()
            if self._container_name:
                docker_client = docker.from_env()
                docker_client.containers.get(self._container_name).remove(force=True)
            return True
        except Exception as e:
            logger.error(f"Failed to destroy Docker sandbox: {str(e)}")
            return False
    
    async def get_browser(self) -> Browser:
        """Get browser interface for sandbox
        
        Args:
            llm: LLM instance used for browser automation
            
        Returns:
            Browser: Returns a configured PlaywrightBrowser instance
                    connected using the sandbox's CDP URL
        """
        return PlaywrightBrowser(self.cdp_url)

    @classmethod
    async def create(cls, session_id: str) -> Sandbox:
        """Create a new Docker sandbox instance."""
        # This method is now a wrapper around the static _create_task method
        sandbox = cls._create_task(session_id)
        await sandbox.start()
        return sandbox

    @classmethod
    @alru_cache(maxsize=128, typed=True)
    async def get(cls, id: str) -> Optional[Sandbox]:
        """Get Docker sandbox instance by ID"""
        if not id:
            logger.error("Attempted to get sandbox with empty ID.")
            return None

        try:
            docker_client = docker.from_env()
            container = docker_client.containers.get(id)
            ip_address = DockerSandbox._get_container_ip(container)
            
            return DockerSandbox(
                ip=ip_address,
                container_name=id
            )
        except docker.errors.NotFound:
            logger.warning(f"Docker container with ID '{id}' not found.")
            return None
        except Exception as e:
            logger.error(f"Failed to get Docker sandbox by ID '{id}': {e}")
            # Re-raising as a more generic exception might be desired depending on caller error handling
            raise Exception(f"An unexpected error occurred while getting sandbox '{id}': {e}")

    async def start(self) -> None:
        self.logger.info(f"Starting sandbox for session: {self.session_id}")
        try:
            # Set up environment for headed mode if not in production
            extra_docker_args = {}
            if os.getenv("ENV") != "production":
                extra_docker_args = {
                    "environment": [f"DISPLAY={os.getenv('DISPLAY')}"],
                    "volumes": {"/tmp/.X11-unix": {"bind": "/tmp/.X11-unix", "mode": "rw"}},
                }

            self.container = self.client.containers.run(
                self.image_name,
                detach=True,
                auto_remove=True,
                # network="host", # Use host network to easily connect to the exposed port
                ports={f"{self.sandbox_port}/tcp": None},  # Let Docker assign a random host port
                name=self.container_name,
                **extra_docker_args,
            )
            self._wait_for_container_to_be_running()
            self.host_port = self._get_container_host_port()
        except Exception as e:
            self.logger.error(f"Failed to start Docker sandbox: {str(e)}")

    async def _launch_browser(self, config: dict) -> None:
        self.logger.info("Launching browser in sandbox")
        # In development, run in headed mode for debugging
        is_headless = os.getenv("ENV") == "production"
        browser_config = {
            "headless": is_headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
            **config.get("browser_options", {}),
        }

        if not is_headless:
            # Add viewport settings for headed mode to ensure window size
            browser_config["viewport"] = {"width": 1280, "height": 720}
            self.logger.info("Running in headed mode with viewport 1280x720")

        await self.websocket.send(
            json.dumps(
                {
                    "action": "launch_browser",
                    "params": {"config": browser_config},
                }
            )
        )

        self._browser = await PlaywrightBrowser.create(
            cdp_url=self.cdp_url,
            proxy=proxy
        )
        logger.info(f"[{self.id}] Browser connected successfully")
        
        # Start forwarding trace events
        await self._browser.start_tracing()
        asyncio.create_task(self._forward_trace_events())

    async def _forward_trace_events(self):
        """Continuously forward trace events to the queue."""
        if not self._browser:
            return
        
        try:
            while True:
                event = await self._browser.get_next_trace_event()
                if event:
                    await self._trace_events_queue.put(event)
                else:
                    # Small sleep to prevent busy-waiting if queue is empty
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"[{self.id}] Error in trace forwarding: {e}")
            # You might want to handle reconnection or cleanup logic here
        finally:
            logger.info(f"[{self.id}] Trace forwarding stopped.")

    async def stream_trace(self, websocket):
        """Streams trace events from the queue to the websocket."""
        logger.info(f"[{self.id}] Starting trace stream.")
        try:
            while True:
                try:
                    event = await asyncio.wait_for(self._trace_events_queue.get(), timeout=1.0)
                    await websocket.send_json(event)
                except asyncio.TimeoutError:
                    # Send a heartbeat if no event is available
                    await websocket.send_json({"type": "heartbeat"})
                except asyncio.QueueEmpty:
                    # This case should ideally be covered by the timeout
                    continue
        except Exception as e:
            logger.error(f"[{self.id}] Error in websocket trace stream: {e}")
        finally:
            logger.info(f"[{self.id}] Trace stream stopped.")
