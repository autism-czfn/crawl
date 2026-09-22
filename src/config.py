import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_URL_SYNC: str = os.getenv("DATABASE_URL_SYNC", "")

    REDDIT_CLIENT_ID: str = os.getenv("REDDIT_CLIENT_ID", "")
    REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")

    PUBMED_API_KEY: str = os.getenv("PUBMED_API_KEY", "")
    SEMANTIC_SCHOLAR_API_KEY: str = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    CORE_API_KEY: str = os.getenv("CORE_API_KEY", "")
    ELSEVIER_API_KEY: str = os.getenv("ELSEVIER_API_KEY", "")
    SPRINGER_API_KEY: str = os.getenv("SPRINGER_API_KEY", "")

    YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")
    NEWSAPI_KEY: str = os.getenv("NEWSAPI_KEY", "")

    CRAWLER_EMAIL: str = os.getenv("CRAWLER_EMAIL", "autism-crawler@example.com")
    # Embeddings use fastembed (local, no API key). Model is hardcoded in src/embedder.py.

    REDIS_URL: str = os.getenv("REDIS_URL", "")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Health check server — polled by the LAN service-monitor (~/data/code/monitor).
    HEALTH_HOST: str = os.getenv("CRAWLER_HEALTH_HOST", "0.0.0.0")
    HEALTH_PORT: int = int(os.getenv("CRAWLER_HEALTH_PORT", "8010"))

    USER_AGENT: str = f"autism-crawler/1.0 (mailto:{os.getenv('CRAWLER_EMAIL', 'autism-crawler@example.com')})"
    BROWSER_USER_AGENT: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


settings = Settings()
