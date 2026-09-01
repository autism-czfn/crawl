from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, Text, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from pgvector.sqlalchemy import Vector
# JSONB already imported above; no extra import needed for content_fingerprint


class Base(DeclarativeBase):
    pass


class CrawledItem(Base):
    __tablename__ = "crawled_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(Text, nullable=True)
    source = Column(Text, nullable=False)
    surface_key = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    content_body = Column(Text, nullable=True)
    author = Column(Text, nullable=True)
    authors_json = Column(JSONB, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    collected_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    lang = Column(Text, nullable=True, default="en")
    rank_position = Column(Integer, nullable=True)
    engagement = Column(JSONB, nullable=True)
    doi = Column(Text, nullable=True)
    journal = Column(Text, nullable=True)
    open_access = Column(Boolean, nullable=True)
    authority_tier = Column(Integer, nullable=True)
    source_type = Column(Text, nullable=True)
    audience_type = Column(Text, nullable=True)
    content_hash = Column(Text, nullable=True)
    content_updated_at = Column(DateTime(timezone=True), nullable=True)
    raw_payload = Column(JSONB, nullable=True)
    embedding = Column(Vector(768), nullable=True)
    embedding_model = Column(Text, nullable=True)
    embedded_at = Column(DateTime(timezone=True), nullable=True)
    oa_url = Column(Text, nullable=True)
    last_harvested_at = Column(DateTime(timezone=True), nullable=True)
    # Sprint 1-C: re-chunk trigger
    needs_rechunk = Column(Boolean, nullable=False, default=False)
    # Sprint 2-F: staleness flag
    is_stale = Column(Boolean, nullable=True, default=False)
    # Sprint 3-C: evidence level
    evidence_level = Column(Text, nullable=True)
    # Sprint 3-B: near-duplicate fingerprint
    content_fingerprint = Column(JSONB, nullable=True)
    # P2-2: near-duplicate detection
    near_duplicate_of = Column(Text, nullable=True)   # URL of canonical item if near-dup detected
    # P2-3: embedding schema version
    embedding_schema_version = Column(Text, nullable=True)
    # Sleep/eating/adhd expansion: multi-domain classification. Lists of
    # strings stored as JSONB (matching content_fingerprint's existing
    # convention), e.g. domain_tags=["adhd","autism"] for a comorbidity
    # paper matched by more than one surface — merged, not overwritten, on
    # upsert (see pipeline.py::_merge_tag_list).
    domain_tags = Column(JSONB, nullable=True)
    topic_tags = Column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("url", name="uq_crawled_items_url"),
        Index("idx_doi", "doi", unique=True, postgresql_where="doi IS NOT NULL"),
        Index("idx_source_time", "source", "collected_at"),
        Index("idx_surface_time", "surface_key", "collected_at"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawled_item_id = Column(Integer, nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    embedding = Column(Vector(768), nullable=True)
    embedding_model = Column(Text, nullable=True)
    embedded_at = Column(DateTime(timezone=True), nullable=True)
    # Metadata columns (Sprint 1-B)
    title = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    domain = Column(Text, nullable=True)
    source_type = Column(Text, nullable=True)
    authority_tier = Column(Integer, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    lang = Column(Text, nullable=True)
    # Parent-child retrieval (Sprint 4-A)
    is_summary = Column(Boolean, nullable=True, default=False)
    # Heading-aware chunking (P1-1)
    section_heading = Column(Text, nullable=True)
    heading_path = Column(Text, nullable=True)   # e.g. "Symptoms > Sleep Disorders"
    # P2-3: embedding schema version
    embedding_schema_version = Column(Text, nullable=True)
    # Sleep/eating/adhd expansion: propagated from the parent CrawledItem at
    # chunk-insert time, same as authority_tier/source_type above.
    domain_tags = Column(JSONB, nullable=True)
    topic_tags = Column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("crawled_item_id", "chunk_index", name="uq_chunk_item_index"),
        Index("idx_chunk_item", "crawled_item_id", "chunk_index"),
    )


class Surface(Base):
    __tablename__ = "surfaces"

    key = Column(Text, primary_key=True)
    platform = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    poll_interval_sec = Column(Integer, nullable=False, default=3600)
    max_items_per_run = Column(Integer, nullable=False, default=30)
    config_json = Column(JSONB, nullable=True)
    authority_tier = Column(Integer, nullable=True)
    source_type = Column(Text, nullable=True)
    audience_type = Column(Text, nullable=True)
    language = Column(Text, nullable=True, default="en")
    country = Column(Text, nullable=True)
    organization_name = Column(Text, nullable=True)
    force_recrawl = Column(Boolean, nullable=False, default=False)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_cursor = Column(Text, nullable=True)
    last_status = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    last_run_count = Column(Integer, nullable=True)
    consecutive_fails = Column(Integer, nullable=False, default=0)
    overrides_json = Column(JSONB, nullable=True)
    # Sleep/eating/adhd expansion: static per-surface default, synced from
    # surfaces.json exactly like authority_tier/source_type/audience_type
    # above, then propagated to each crawled_item by pipeline.py.
    domain_tags = Column(JSONB, nullable=True)
    topic_tags = Column(JSONB, nullable=True)


class HttpCache(Base):
    __tablename__ = "http_cache"

    url_hash = Column(Text, primary_key=True)
    etag = Column(Text, nullable=True)
    last_modified = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)


class BlockedDomain(Base):
    """Durable per-domain "give up" tracking for enrich_fulltext() — see
    migration 0021_add_blocked_domains for the rationale."""
    __tablename__ = "blocked_domains"

    domain = Column(Text, primary_key=True)
    consecutive_403_count = Column(Integer, nullable=False, default=0)
    given_up = Column(Boolean, nullable=False, default=False)
    given_up_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class DiscoveryQueueState(Base):
    """Durable round-robin cursor for discovery_loop() — see migration
    0022_add_discovery_queue_state for the rationale. Singleton row
    (id=1): one discovery_loop, one cursor."""
    __tablename__ = "discovery_queue_state"

    id = Column(Integer, primary_key=True)
    last_index = Column(Integer, nullable=False, default=-1)
    last_pair_domain = Column(Text, nullable=True)
    last_pair_topic = Column(Text, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)


class SearchDiscoveryRequest(Base):
    """Shared DB-as-event-broker table between search repo and crawl —
    see migration 0023_search_discovery_requests and websearch.txt
    sections 十/十五/十九. search repo writes rows directly via its own
    asyncpg pool (no HTTP API); search_queue_loop.py polls and processes
    them here.

    Column contract: url/title/snippet/source_domain/trigger_query are
    what search writes — renaming/retyping those needs coordinating with
    search. retry_count/last_http_status/next_retry_at/error_note/
    processed_at/classifier_tier/classifier_confidence/classifier_reason/
    promoted_surface_key are crawl-internal (search_queue_loop.py only);
    crawl can change these freely.

    classifier_tier/classifier_confidence/classifier_reason/
    promoted_surface_key (migration 0024) record the
    src/discovery/classifier.py verdict for a row whose domain wasn't
    already tier1/2 — set regardless of whether that verdict led to a
    promotion, so an out_of_scope row's reason is visible in the
    setup.sh option-10 report instead of being silent (websearch.txt
    section 十九). promoted_surface_key is the config/surfaces.json key
    the domain was written under (src/discovery/surfaces_writer.py), null
    if it was never promoted (rejected, or a classifier call that never
    ran because the daily cap was already hit).
    """
    __tablename__ = "search_discovery_requests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(Text, nullable=False, unique=True)
    title = Column(Text, nullable=True)
    snippet = Column(Text, nullable=True)
    source_domain = Column(Text, nullable=True)
    trigger_query = Column(Text, nullable=True)
    discovered_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    status = Column(Text, nullable=False, default="pending")
    processed_at = Column(DateTime(timezone=True), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    last_http_status = Column(Integer, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    error_note = Column(Text, nullable=True)
    classifier_tier = Column(Integer, nullable=True)
    classifier_confidence = Column(Text, nullable=True)
    classifier_reason = Column(Text, nullable=True)
    promoted_surface_key = Column(Text, nullable=True)
