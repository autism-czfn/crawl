from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from src.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,       # default 5 → 10; handles burst of concurrent surface runs
    max_overflow=20,    # default 10 → 20; allows short spikes without pool-timeout errors
    pool_timeout=60,    # default 30s → 60s; gives more time during heavy startup bursts
    # command_timeout bounds a single query's wall-clock time on the wire.
    # Without it, a connection whose socket died silently (e.g. the host
    # sleeping mid-query — confirmed root cause 2026-09-25: scheduler's and
    # health.py's article-count queries hung for ~18-20h after a sleep/wake,
    # since neither the OS nor asyncpg raised anything on the dead socket)
    # awaits forever instead of erroring, wedging whichever loop holds it.
    # pool_pre_ping only re-validates a connection at CHECKOUT time, so it
    # doesn't help a query already in flight when the socket dies -- this is
    # the piece that actually bounds that case. 30s gives ~100x headroom over
    # the slowest real query observed here (full crawled_items scan, ~0.1s
    # at 100k rows) while still failing fast enough for the loop's own
    # try/except + heartbeat to recover on the next cycle instead of hanging.
    connect_args={"command_timeout": 30},
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Second, independent engine for search_discovery_requests, which now
# lives in search's own isolated search_discovery_queue database (crawl.txt
# §4/§10) instead of this primary DB. Deliberately separate from `engine`
# above: this is a remote connection to a different host, to a small
# single-table queue DB, via a role (search_discovery_consumer) that has
# nothing to do with crawled_items/Surface. Both stay None if
# SEARCH_QUEUE_DATABASE_URL is unset, so a deployment that hasn't been
# wired up to the new DB yet doesn't crash at import time — it just can't
# run search_queue_loop() yet (see that module's fail-closed guard).
search_queue_engine = (
    create_async_engine(
        settings.SEARCH_QUEUE_DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,        # one poller loop, one small table — no need for engine's larger pool
        max_overflow=5,
        pool_timeout=30,
        connect_args={"command_timeout": 30},  # same rationale as engine's above: bound a hung remote query
    )
    if settings.SEARCH_QUEUE_DATABASE_URL
    else None
)
SearchQueueSessionLocal = (
    async_sessionmaker(search_queue_engine, class_=AsyncSession, expire_on_commit=False)
    if search_queue_engine is not None
    else None
)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
