from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Integer, Text, UniqueConstraint, Index
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


class HttpCache(Base):
    __tablename__ = "http_cache"

    url_hash = Column(Text, primary_key=True)
    etag = Column(Text, nullable=True)
    last_modified = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
