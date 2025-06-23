from typing import Optional, AsyncGenerator
import logging
import time
from datetime import datetime
from app.domain.models.session import Session, SessionStatus
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.events.agent_events import BaseEvent, ErrorEvent, DoneEvent, PlanEvent, StepEvent, ToolEvent, MessageEvent, AgentEventFactory
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.external.task import Task
from app.domain.utils.json_parser import JsonParser
from typing import Type

# Setup logging
logger = logging.getLogger(__name__)

class AgentDomainService:
    """
    Agent domain service, responsible for coordinating the work of planning agent and execution agent
    """
    
    def __init__(
        self,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        llm: LLM,
        sandbox_cls: Type[Sandbox],
        task_cls: Type[Task],
        json_parser: JsonParser,
        search_engine: Optional[SearchEngine] = None
    ):
        self._repository = agent_repository
        self._session_repository =session_repository
        self._llm = llm
        self._sandbox_cls = sandbox_cls
        self._search_engine = search_engine
        self._task_cls = task_cls
        self._json_parser = json_parser
        logger.info("AgentDomainService initialization completed")
            
    async def shutdown(self) -> None:
        """Clean up all Agent's resources"""
        logger.info(f"Starting to close all Agents")
        await self._task_cls.destroy()
        logger.info("All agents closed successfully")

    async def _get_or_create_sandbox_for_session(self, session: Session) -> Sandbox:
        """Get or create a sandbox for the given session."""
        sandbox = None
        sandbox_id = session.sandbox_id
        if sandbox_id:
            sandbox = await self._sandbox_cls.get(sandbox_id)
        if not sandbox:
            logger.info(f"No sandbox found for session {session.id}, creating a new one.")
            sandbox = await self._sandbox_cls.create(session_id=session.id)
            session.sandbox_id = sandbox.id
            await self._session_repository.save(session)
            logger.info(f"New sandbox {sandbox.id} created and associated with session {session.id}.")
        return sandbox

    async def _create_task(self, session: Session) -> Task:
        """Create a new agent task"""
        sandbox = await self._get_or_create_sandbox_for_session(session)
        browser = await sandbox.get_browser()
        if not browser:
            logger.error(f"Failed to get browser for Sandbox {sandbox.id}")
            raise RuntimeError(f"Failed to get browser for Sandbox {sandbox.id}")
        
        await self._session_repository.save(session)

        task_runner = AgentTaskRunner(
            session_id=session.id,
            agent_id=session.agent_id,
            llm=self._llm,
            sandbox=sandbox,
            browser=browser,
            search_engine=self._search_engine,
            session_repository=self._session_repository,
            json_parser=self._json_parser,
            agent_repository=self._repository,
        )

        task = self._task_cls.create(task_runner)
        session.task_id = task.id
        await self._session_repository.save(session)

        return task
        
    async def _get_task(self, session: Session) -> Optional[Task]:
        """Get a task for the given session"""

        task_id = session.task_id
        if not task_id:
            return None
        
        return self._task_cls.get(task_id)

    async def stop_session(self, session_id: str) -> None:
        """Stop a session"""
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Attempted to stop non-existent Session {session_id}")
            raise RuntimeError("Session not found")
        task = await self._get_task(session)
        if task:
            task.cancel()
        await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)

    async def chat(
        self,
        session_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        latest_event_id: Optional[str] = None
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        Chat with an agent
        """

        try:
            session = await self._session_repository.find_by_id(session_id)
            if not session:
                logger.error(f"Attempted to chat with non-existent Session {session_id}")
                raise RuntimeError("Session not found")

            task = await self._get_task(session)

            if message:
                if task:
                    task.cancel()
                task = await self._create_task(session)
                if not task:
                    raise RuntimeError("Failed to create task")
                
                await self._session_repository.update_latest_message(session_id, message, timestamp or datetime.now())

                message_id = await task.input_stream.put(message)
                message_event = MessageEvent(message=message, role="user", id=message_id)
                await self._session_repository.add_event(session_id, message_event)
                await task.run()
                logger.debug(f"Put message into Session {session_id}'s event queue: {message[:50]}...")
            
            logger.info(f"Session {session_id} started")
            logger.debug(f"Session {session_id} task: {task}")
           
            while task and not task.done:
                event_id, event_str = await task.output_stream.get(start_id=latest_event_id, block_ms=0)
                latest_event_id = event_id
                if event_str is None:
                    logger.debug(f"No event found in Session {session_id}'s event queue")
                    continue
                event = AgentEventFactory.from_json(event_str)
                event.id = event_id
                logger.debug(f"Got event from Session {session_id}'s event queue: {type(event).__name__}")
                await self._session_repository.update_unread_message_count(session_id, 0)
                yield event
                if isinstance(event, (DoneEvent, ErrorEvent)):
                    break
            
            logger.info(f"Session {session_id} completed")

        except Exception as e:
            logger.exception(f"Error in Session {session_id}")
            event = ErrorEvent(error=str(e))
            await self._session_repository.add_event(session_id, event)
            yield event # TODO: raise api exception
        finally:
            await self._session_repository.update_unread_message_count(session_id, 0)