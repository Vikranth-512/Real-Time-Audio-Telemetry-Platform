import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert
import logging
from .models import Base, AudioMetric

logger = logging.getLogger(__name__)

# Debug flag for verbose logging (set via environment variable)
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://user:password@localhost:5432/audio_db")

engine = create_async_engine(
    POSTGRES_URL, 
    echo=False, 
    pool_size=20, 
    max_overflow=10
)

async_session = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def batch_insert_metrics(metrics: list[dict]):
    if not metrics:
        return
    
    async with async_session() as session:
        try:
            # Debug: Log session IDs being inserted
            session_ids = set(m['session_id'] for m in metrics)
            if DEBUG_MODE:
                logger.info(f"Inserting {len(metrics)} metrics for sessions: {session_ids}")
            
            stmt = insert(AudioMetric).values(metrics)
            # Ignore duplicates based on UNIQUE(device_id, timestamp, session_id)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=['device_id', 'timestamp', 'session_id']
            )
            result = await session.execute(stmt)
            await session.commit()
            
            if DEBUG_MODE:
                logger.info(f"Successfully inserted {result.rowcount} metrics")
                
        except Exception as e:
            logger.error(f"Failed to batch insert metrics: {e}")
            await session.rollback()
            raise
