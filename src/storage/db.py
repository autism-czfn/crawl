from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from src.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,       # default 5 → 10; handles burst of concurrent surface runs
    max_overflow=20,    # default 10 → 20; allows short spikes without pool-timeout errors
    pool_timeout=60,    # default 30s → 60s; gives more time during heavy startup bursts
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
