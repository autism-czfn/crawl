"""Full-text fetching utilities for open-access academic sources.

Provides async helpers that attempt to retrieve full article text beyond
what the base APIs return (abstracts only).  Each function is best-effort:
returns None on any failure so callers can gracefully fall back to abstract.

Strategies by source:
  PMC       — fetch HTML from pmc.ncbi.nlm.nih.gov, extract with trafilatura
  EuropePMC — full-text XML API, walk all text nodes
  Semantic Scholar — fetch open-access PDF URL, extract with pdfplumber
  CORE      — fetch downloadUrl PDF if API fullText field is absent/short
  bioRxiv   — fetch HTML full text from biorxiv.org/medrxiv.org
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_MIN_CHARS = 300          # discard anything shorter
_MAX_CHARS = 50_000       # cap stored text


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clean(text: str) -> str | None:
    """Strip whitespace and enforce min/max length."""
    text = " ".join(text.split()).strip()
    if len(text) < _MIN_CHARS:
        return None
    return text[:_MAX_CHARS]


async def _fetch_bytes(client, url: str, timeout: int = 30) -> bytes | None:
    """GET url → raw bytes, or None on any error."""
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.debug("fetch failed %s: %s", url, exc)
        return None


def _extract_html_text(html: bytes) -> str | None:
    """Extract main article body from HTML using trafilatura."""
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        return _clean(text) if text else None
    except Exception as exc:
        logger.debug("trafilatura extraction failed: %s", exc)
        return None


def _extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Extract text from PDF bytes using pdfplumber."""
    from src.extractors.pdf import extract_text_from_pdf
    text = extract_text_from_pdf(pdf_bytes)
    if not text:
        return None
    return _clean(text)


def _extract_xml_text(xml_bytes: bytes) -> str | None:
    """Walk an XML tree and collect all non-empty text nodes."""
    try:
        root = ET.fromstring(xml_bytes)
        parts: list[str] = []
        for el in root.iter():
            if el.text and el.text.strip():
                parts.append(el.text.strip())
            if el.tail and el.tail.strip():
                parts.append(el.tail.strip())
        return _clean(" ".join(parts))
    except Exception as exc:
        logger.debug("XML text extraction failed: %s", exc)
        return None


# ── Public fetch functions ────────────────────────────────────────────────────

async def fetch_pmc_fulltext(client, pmcid: str) -> str | None:
    """Fetch full text for a PMC article.

    Tries HTML first (best quality), falls back to efetch XML.
    pmcid should be numeric or 'PMCxxxxxxx' — both forms accepted.
    """
    numeric_id = pmcid.replace("PMC", "").strip()

    # 1. HTML via PMC reader (trafilatura handles it well)
    html_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{numeric_id}/"
    html = await _fetch_bytes(client, html_url)
    if html:
        text = _extract_html_text(html)
        if text:
            logger.debug("PMC full text via HTML for PMC%s (%d chars)", numeric_id, len(text))
            return text

    # 2. efetch XML fallback
    from src.config import settings
    params = f"db=pmc&id={numeric_id}&rettype=full&retmode=xml"
    if settings.PUBMED_API_KEY:
        params += f"&api_key={settings.PUBMED_API_KEY}"
    xml_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{params}"
    xml_bytes = await _fetch_bytes(client, xml_url)
    if xml_bytes:
        text = _extract_xml_text(xml_bytes)
        if text:
            logger.debug("PMC full text via efetch XML for PMC%s (%d chars)", numeric_id, len(text))
            return text

    logger.debug("No full text available for PMC%s", numeric_id)
    return None


async def fetch_europepmc_fulltext(client, source: str, paper_id: str) -> str | None:
    """Fetch full text from EuropePMC XML API.

    source: 'MED' (PubMed), 'PMC', 'PPR' (preprint), etc.
    paper_id: the pmid, pmcid, or ppr id.
    """
    xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{paper_id}/fullTextXML"
    xml_bytes = await _fetch_bytes(client, xml_url)
    if not xml_bytes:
        return None
    text = _extract_xml_text(xml_bytes)
    if text:
        logger.debug("EuropePMC full text for %s/%s (%d chars)", source, paper_id, len(text))
    return text


async def fetch_html_and_extract(client, url: str) -> str | None:
    """Fetch an HTML page and extract main article text via trafilatura."""
    html = await _fetch_bytes(client, url)
    if not html:
        return None
    text = _extract_html_text(html)
    if text:
        logger.debug("HTML full text from %s (%d chars)", url, len(text))
    return text


async def fetch_pdf_url(client, pdf_url: str) -> str | None:
    """Fetch and extract text from a PDF at the given URL."""
    pdf_bytes = await _fetch_bytes(client, pdf_url, timeout=60)
    if not pdf_bytes:
        return None
    text = _extract_pdf_text(pdf_bytes)
    if text:
        logger.debug("PDF extracted from %s (%d chars)", pdf_url, len(text))
    return text


async def fetch_biorxiv_fulltext(client, doi: str, server: str = "biorxiv") -> str | None:
    """Fetch full text for a bioRxiv/medRxiv preprint.

    Tries HTML first (trafilatura), then PDF fallback.
    doi should be bare, e.g. '10.1101/2024.01.01.123456'
    """
    base_domain = "medrxiv" if server == "medrxiv" else "biorxiv"

    # 1. HTML full text
    html_url = f"https://www.{base_domain}.org/content/{doi}"
    html = await _fetch_bytes(client, html_url)
    if html:
        text = _extract_html_text(html)
        if text:
            logger.debug("bioRxiv HTML full text for %s (%d chars)", doi, len(text))
            return text

    # 2. PDF fallback
    pdf_url = f"https://www.{base_domain}.org/content/{doi}.full.pdf"
    text = await fetch_pdf_url(client, pdf_url)
    if text:
        logger.debug("bioRxiv PDF full text for %s (%d chars)", doi, len(text))
    return text
