"""Read-only Django models that map to existing SQLAlchemy-managed tables.

managed = False means Django never creates, alters, or drops these tables.
SQLAlchemy + Alembic own all schema changes.
"""
from django.db import models


class Surface(models.Model):
    """Crawler data source (one row per configured collector)."""

    key               = models.TextField(primary_key=True)
    platform          = models.TextField()
    enabled           = models.BooleanField(default=True)
    poll_interval_sec = models.IntegerField(default=3600)
    max_items_per_run = models.IntegerField(default=30)
    config_json       = models.JSONField(null=True, blank=True)
    last_run_at       = models.DateTimeField(null=True, blank=True)
    last_cursor       = models.TextField(null=True, blank=True)
    last_status       = models.TextField(null=True, blank=True)   # ok | error | blocked | empty
    last_error        = models.TextField(null=True, blank=True)
    last_run_count    = models.IntegerField(null=True, blank=True)
    consecutive_fails = models.IntegerField(default=0)
    force_recrawl     = models.BooleanField(default=False)

    class Meta:
        managed  = False
        db_table = "surfaces"
        ordering = ["platform", "key"]

    def __str__(self) -> str:
        return self.key


class CrawledItem(models.Model):
    """A single piece of content collected by a crawler surface."""

    id            = models.AutoField(primary_key=True)
    external_id   = models.TextField(null=True, blank=True)
    source        = models.TextField()
    surface_key   = models.TextField()
    title         = models.TextField()
    url           = models.TextField(unique=True)
    description   = models.TextField(null=True, blank=True)
    content_body  = models.TextField(null=True, blank=True)
    author        = models.TextField(null=True, blank=True)
    authors_json  = models.JSONField(null=True, blank=True)
    published_at  = models.DateTimeField(null=True, blank=True)
    collected_at  = models.DateTimeField()
    lang          = models.TextField(null=True, blank=True, default="en")
    rank_position = models.IntegerField(null=True, blank=True)
    engagement    = models.JSONField(null=True, blank=True)
    doi           = models.TextField(null=True, blank=True)
    journal       = models.TextField(null=True, blank=True)
    open_access   = models.BooleanField(null=True, blank=True)
    raw_payload   = models.JSONField(null=True, blank=True)
    # embedding column (vector type) intentionally omitted — not renderable in admin
    embedding_model = models.TextField(null=True, blank=True)
    embedded_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed  = False
        db_table = "crawled_items"
        ordering = ["-collected_at"]

    def __str__(self) -> str:
        return self.title


class HttpCache(models.Model):
    """HTTP conditional-request cache (ETags / Last-Modified headers)."""

    url_hash      = models.TextField(primary_key=True)
    etag          = models.TextField(null=True, blank=True)
    last_modified = models.TextField(null=True, blank=True)
    expires_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed  = False
        db_table = "http_cache"

    def __str__(self) -> str:
        return self.url_hash
