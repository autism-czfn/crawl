from django.contrib import admin
from django.utils.html import format_html

from .models import CrawledItem, HttpCache, Surface


# ── Surface admin ────────────────────────────────────────────────────────────

@admin.register(Surface)
class SurfaceAdmin(admin.ModelAdmin):
    list_display  = [
        "key", "platform", "enabled_badge", "force_recrawl", "last_status_badge",
        "last_run_count", "consecutive_fails", "last_run_at", "short_error",
    ]
    list_filter   = ["platform", "enabled", "force_recrawl", "last_status"]
    search_fields = ["key", "platform"]
    ordering      = ["-consecutive_fails", "last_status", "key"]
    readonly_fields = [
        "key", "platform", "last_run_at", "last_cursor",
        "last_status", "last_error", "last_run_count", "consecutive_fails",
        "config_json",
    ]
    fields = [
        "key", "platform", "enabled", "force_recrawl",
        "poll_interval_sec", "max_items_per_run", "config_json",
        "last_run_at", "last_status", "last_error",
        "last_run_count", "consecutive_fails", "last_cursor",
    ]

    @admin.display(description="Enabled", boolean=False)
    def enabled_badge(self, obj: Surface) -> str:
        if obj.enabled:
            return format_html('<span style="color:green">✔ yes</span>')
        return format_html('<span style="color:#aaa">✘ no</span>')

    @admin.display(description="Status")
    def last_status_badge(self, obj: Surface) -> str:
        colour = {
            "ok":      "green",
            "error":   "red",
            "blocked": "orange",
            "empty":   "#888",
        }.get(obj.last_status or "", "#888")
        label = obj.last_status or "—"
        return format_html('<span style="color:{}">{}</span>', colour, label)

    @admin.display(description="Last error")
    def short_error(self, obj: Surface) -> str:
        if obj.last_error:
            return obj.last_error[:80] + ("…" if len(obj.last_error) > 80 else "")
        return "—"


# ── CrawledItem admin ────────────────────────────────────────────────────────

@admin.register(CrawledItem)
class CrawledItemAdmin(admin.ModelAdmin):
    list_display  = [
        "title_truncated", "source", "surface_key",
        "open_access", "published_at", "collected_at",
    ]
    list_filter   = ["source", "surface_key", "open_access", "lang"]
    search_fields = ["title", "doi", "author", "url"]
    ordering      = ["-collected_at"]
    readonly_fields = [
        "id", "external_id", "source", "surface_key", "url",
        "doi", "journal", "open_access", "author", "authors_json",
        "published_at", "collected_at", "lang", "rank_position",
        "engagement", "raw_payload", "embedding_model", "embedded_at",
    ]
    fields = [
        "id", "title", "url", "source", "surface_key",
        "description", "content_body",
        "author", "authors_json",
        "doi", "journal", "open_access",
        "published_at", "collected_at", "lang",
        "rank_position", "engagement",
        "external_id", "raw_payload",
        "embedding_model", "embedded_at",
    ]

    @admin.display(description="Title")
    def title_truncated(self, obj: CrawledItem) -> str:
        t = obj.title or ""
        return t[:80] + ("…" if len(t) > 80 else "")


# ── HttpCache admin ───────────────────────────────────────────────────────────

@admin.register(HttpCache)
class HttpCacheAdmin(admin.ModelAdmin):
    list_display  = ["url_hash", "etag", "last_modified", "expires_at"]
    search_fields = ["url_hash", "etag"]
    ordering      = ["url_hash"]
    readonly_fields = ["url_hash", "etag", "last_modified", "expires_at"]
