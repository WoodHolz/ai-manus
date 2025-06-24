from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio

from app.interfaces.api.router import router as core_api_router
from app.interfaces.api.routes.session import router as session_router
from app.dependencies import agent_service_instance as agent_service
from app.infrastructure.config import get_settings
from app.infrastructure.logging import setup_logging
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.storage.redis import get_redis
from app.infrastructure.external.search.google_search import GoogleSearchEngine
from app.infrastructure.external.llm.openai_llm import OpenAILLM
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.repositories.mongo_agent_repository import MongoAgentRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.external.task.redis_task import RedisStreamTask
from app.infrastructure.models.documents import AgentDocument, SessionDocument
from app.infrastructure.utils.llm_json_parser import LLMJsonParser
from beanie import init_beanie

# Initialize logging system
setup_logging()
logger = logging.getLogger(__name__)

# Load configuration
settings = get_settings()

async def shutdown() -> None:
    """Cleanup function that will be called when the application is shutting down"""
    logger.info("Graceful shutdown...")
    
    try:
        # Create task for agent service shutdown with timeout
        shutdown_task = asyncio.create_task(agent_service.shutdown())
        try:
            await asyncio.wait_for(shutdown_task, timeout=30.0)  # 30 seconds timeout
            logger.info("Successfully completed graceful shutdown")
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out after 30 seconds")
            
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}")

# Create lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Code executed on startup
    logger.info("Application startup - Manus AI Agent initializing")
    
    # Initialize MongoDB and Beanie
    await get_mongodb().initialize()

    # Initialize Beanie
    await init_beanie(
        database=get_mongodb().client[settings.mongodb_database],
        document_models=[AgentDocument, SessionDocument]
    )
    logger.info("Successfully initialized Beanie")
    
    # Initialize Redis
    await get_redis().initialize()
    
    try:
        yield
    finally:
        # Code executed on shutdown
        logger.info("Application shutdown - Manus AI Agent terminating")
        # Disconnect from MongoDB
        await get_mongodb().shutdown()
        # Disconnect from Redis
        await get_redis().shutdown()
        await shutdown()

# Create FastAPI app
app = FastAPI(title="Manus AI Agent", lifespan=lifespan, timeout_graceful_shutdown=5)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register exception handlers
register_exception_handlers(app)

# Create a main router
api_v1_router = APIRouter()

# Include all the different routers
api_v1_router.include_router(core_api_router) # The original core APIs
api_v1_router.include_router(session_router, prefix="/session", tags=["session"]) # Our new router

# Mount the main router
app.include_router(api_v1_router, prefix="/api/v1")

@app.on_event("startup")
async def startup_event():
    """Application startup event"""
    settings = get_settings()
    if settings.mongodb_uri:
        await get_mongodb().connect(
            uri=settings.mongodb_uri,
            db_name=settings.mongodb_database
        )
    if settings.redis_host:
        await get_redis().connect(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password
        )

@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown event"""
    settings = get_settings()
    if settings.mongodb_uri:
        await get_mongodb().disconnect()
    if settings.redis_host:
        await get_redis().disconnect()