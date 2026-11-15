"""Import scene news/infos for hardcore, uptempo, hardstyle, gabber into news & knowledge DB.

Curated source list (researched public websites/magazines/community hubs). Fetching uses
RSS/XML when available and lightweight HTML title extraction as fallback. Stores:
- news_sources (seeded, idempotent)
- news_items (dedup by url/title hash best-effort)
- knowledge_entries (summarized/import records for content knowledge base)

No invented facts/content; only fetched/public metadata is stored.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from database import knowledge_upsert, news_add_item, news_list_items, news_list_sources, news_upsert_source, write_log

logger = logging.getLogger(__name__)

# Curated public sources (hardcore/uptempo/hardstyle/gabber scene). RSS preferred.
CURATED_SOURCES: list[dict[str, Any]] = [
    {"name": "Hard News (EN)", "url": "https://hardnews.nl/en/", "rss": "https://hardnews.nl/en/feed/", "category": "hardstyle", "scene": "hardstyle/raw/uptempo/hardcore"},
    {"name": "Hard News (NL)", "url": "https://hardnews.nl/", "rss": "https://hardnews.nl/feed/", "category": "hardstyle", "scene": "hardstyle/raw/uptempo/hardcore"},
    {"name": "Hardstyle Mag", "url": "https://hardstylemag.com/", "rss": "https://hardstylemag.com/feed/", "category": "hardstyle", "scene": "hardstyle/hardcore/festival"},
    {"name": "HARD CULTR Magazine", "url": "https://www.hardcultr.com/", "rss": "", "category": "hard-dance", "scene": "hardstyle/hardcore/uptempo/frenchcore"},
    {"name": "Real Hardstyle News", "url": "http://www.realhardstyle.com/community/news", "rss": "", "category": "hardstyle", "scene": "hardstyle/raw/uptempo"},
    {"name": "Hardstyle.com News", "url": "https://news.hardstyle.com/en", "rss": "", "category": "hardstyle", "scene": "hardstyle/releases/events"},
    {"name": "Partyflock Agenda Hardcore", "url": "https://partyflock.nl/agenda/genre/hardcore", "rss": "", "category": "events", "scene": "hardcore/gabber/events"},
    {"name": "Partyflock Agenda Hardstyle", "url": "https://partyflock.nl/agenda/genre/hardstyle", "rss": "", "category": "events", "scene": "hardstyle/events"},
    {"name": "Hardcore Radio / Streams Hub", "url": "https://hardstreams.nl/", "rss": "", "category": "radio", "scene": "hardcore/gabber/hardstyle/uptempo"},
    {"name": "Masters of Hardcore (News/Events)", "url": "https://www.mastersofhardcore.com/", "rss": "", "category": "hardcore", "scene": "hardcore/gabber"},
    {"name": "Thunderdome", "url": "https://www.thunderdome.com/", "rss": "", "category": "hardcore", "scene": "gabber/hardcore"},
    {"name": "Dominator Festival", "url": "https://www.dominatorfestival.com/", "rss": "", "category": "events", "scene": "hardcore/uptempo"},
    {"name": "Defqon.1", "url": "https://www.q-dance.com/en/events/defqon-1/", "rss": "", "category": "events", "scene": "hardstyle/raw/uptempo/hardcore"},
    {"name": "Q-dance", "url": "https://www.q-dance.com/", "rss": "", "category": "hardstyle", "scene": "hardstyle/events"},
    {"name": "Art of Dance", "url": "https://www.artofdance.nl/", "rss": "", "category": "events", "scene": "hardstyle/raw/hardcore"},
    {"name": "Gabber.nu / Scene Links", "url": "https://gabber.nu/", "rss": "", "category": "gabber", "scene": "gabber/hardcore/community"},
    {"name": "Hardtunes / Hard Dance", "url": "https://www.hardtunes.com/", "rss": "", "category": "music", "scene": "hardstyle/hardcore/releases"},
    {"name": "Scantraxx", "url": "https://www.scantraxx.com/", "rss": "", "category": "label", "scene": "hardstyle/raw"},
    {"name": "Dirty Workz", "url": "https://www.dirtyworkz.com/", "rss": "", "category": "label", "scene": "hardstyle/happy-hard"},
    {"name": "Gearbox Digital", "url": "https://gearboxdigital.com/", "rss": "", "category": "label", "scene": "raw/hardstyle"},
    {"name": "Uptempo/Hardcore Community (Partyflock tag)", "url": "https://partyflock.nl/genre/uptempo", "rss": "", "category": "events", "scene": "uptempo"},
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


async def seed_scene_sources() -> dict[str, Any]:
    created = []
    for src in CURATED_SOURCES:
        rec = await news_upsert_source({
            "name": src["name"],
            "url": src.get("rss") or src.get("url") or "",
            "category": src.get("category") or "scene",
            "enabled": True,
            "config_json": {"homepage": src.get("url"), "rss": src.get("rss", ""), "scene": src.get("scene", ""), "curated": True},
        })
        created.append(rec.get("id") or rec.get("name"))
    await write_log("INFO", "scene.sources_seeded", f"Seeded {len(created)} curated scene sources", {"count": len(created)})
    return {"seeded": len(created), "sources": [s["name"] for s in CURATED_SOURCES]}


def _parse_rss(xml_text: str, source_url: str) -> list[dict[str, Any]]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    # RSS 2.0
    for it in root.findall(".//item"):
        title = _strip_tags((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip() or source_url
        desc = _strip_tags((it.findtext("description") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""))
        pub = (it.findtext("pubDate") or it.findtext("published") or "").strip()
        if title or desc:
            items.append({"title": title[:300], "url": link, "summary": desc[:2000], "published_at": pub})
    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for it in root.findall(".//a:entry", ns):
            title = _strip_tags((it.findtext("a:title", default="", namespaces=ns) or "").strip())
            link_el = it.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or source_url
            desc = _strip_tags((it.findtext("a:summary", default="", namespaces=ns) or it.findtext("a:content", default="", namespaces=ns) or ""))
            pub = (it.findtext("a:updated", default="", namespaces=ns) or it.findtext("a:published", default="", namespaces=ns) or "").strip()
            if title or desc:
                items.append({"title": title[:300], "url": link, "summary": desc[:2000], "published_at": pub})
    return items[:30]


def _extract_html_items(html_text: str, base_url: str) -> list[dict[str, Any]]:
    items = []
    # simple headline extraction
    for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html_text, re.I | re.S):
        href = m.group(1)
        text = _strip_tags(m.group(2))
        if len(text) < 20 or len(text) > 180:
            continue
        if any(x in text.lower() for x in ["impressum", "privacy", "cookie", "login", "kontakt", "menu", "home"]):
            continue
        full = urljoin(base_url, href)
        if full.startswith("http"):
            items.append({"title": text[:300], "url": full, "summary": "", "published_at": ""})
        if len(items) >= 15:
            break
    # fallback: title tag
    if not items:
        tm = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        if tm:
            items.append({"title": _strip_tags(tm.group(1))[:300], "url": base_url, "summary": "", "published_at": ""})
    return items


async def fetch_and_import_source(source: dict[str, Any], *, max_items: int = 20) -> dict[str, Any]:
    cfg = source.get("config_json") or {}
    if isinstance(cfg, str):
        try:
            import json as _json
            cfg = _json.loads(cfg)
        except Exception:
            cfg = {}
    homepage = (cfg.get("homepage") or source.get("url") or "").strip()
    rss = (cfg.get("rss") or "").strip()
    name = source.get("name") or homepage or "source"
    sid = str(source.get("id") or "")
    fetched = 0
    stored_items = 0
    errors = []
    urls_to_try = [u for u in [rss, homepage] if u]
    timeout = httpx.Timeout(15.0, connect=8.0)
    headers = {"User-Agent": "OMNI-CORE-SceneImporter/1.0 (+knowledge-base)"}
    parsed: list[dict[str, Any]] = []
    fetch_url = ""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for u in urls_to_try:
            try:
                r = await client.get(u)
                if r.status_code >= 400:
                    continue
                fetch_url = str(r.url)
                ctype = (r.headers.get("content-type") or "").lower()
                text = r.text or ""
                fetched = len(text)
                if "xml" in ctype or u.endswith(".xml") or "rss" in u or "<rss" in text[:500].lower() or "<feed" in text[:500].lower():
                    parsed = _parse_rss(text, fetch_url)
                if not parsed:
                    parsed = _extract_html_items(text, fetch_url)
                if parsed:
                    break
            except Exception as e:
                errors.append(f"{u}: {e}")
    # dedup existing by url
    existing = await news_list_items(limit=300)
    existing_urls = {str(x.get("url") or "").strip() for x in existing if x.get("url")}
    existing_titles = {_strip_tags(str(x.get("title") or "")).lower() for x in existing if x.get("title")}
    knowledge_chunks = []
    for it in parsed[:max_items]:
        title = _strip_tags(it.get("title") or "")
        url = str(it.get("url") or fetch_url or homepage).strip()
        summary = _strip_tags(it.get("summary") or "")
        if not title and not summary:
            continue
        if url and url in existing_urls:
            continue
        if title and title.lower() in existing_titles:
            continue
        try:
            await news_add_item({
                "source_id": sid,
                "title": title[:300] or (url[:120]),
                "url": url[:1000],
                "summary": summary[:2000],
                "category": str(source.get("category") or cfg.get("scene") or "scene"),
                "published_at": it.get("published_at") or None,
                "metadata_json": {"source_name": name, "fetched_from": fetch_url, "scene": cfg.get("scene"), "imported_at": _utc_now()},
            })
            stored_items += 1
            existing_urls.add(url)
            if title:
                existing_titles.add(title.lower())
            knowledge_chunks.append(f"- {title} ({url})" + (f": {summary[:300]}" if summary else ""))
        except Exception as e:
            errors.append(f"store:{title[:40]}:{e}")

    # knowledge base entry per source (aggregated, factual fetched snippets/links)
    if knowledge_chunks:
        content = f"Szene-Wissen Import ({name}, {cfg.get('scene','hardcore/uptempo/hardstyle/gabber')}), Stand {_utc_now()}\nQuelle: {homepage or fetch_url}\n\n" + "\n".join(knowledge_chunks[:20])
        await knowledge_upsert({
            "title": f"Szene-News: {name}",
            "content": content[:12000],
            "category": "scene_knowledge",
            "tags": ["hardcore", "uptempo", "hardstyle", "gabber", "news", str(source.get("category") or "scene")],
            "source": homepage or fetch_url,
            "metadata_json": {"source_id": sid, "fetched_from": fetch_url, "items_imported": stored_items, "imported_at": _utc_now()},
        })
    return {"source": name, "fetched_bytes": fetched, "parsed": len(parsed), "stored_items": stored_items, "errors": errors[:5], "fetched_from": fetch_url}


async def import_all_scene_news(*, seed_first: bool = True, max_sources: int = 30) -> dict[str, Any]:
    if seed_first:
        await seed_scene_sources()
    sources = await news_list_sources()
    # prefer curated scene sources
    curated_names = {s["name"] for s in CURATED_SOURCES}
    scene_sources = [s for s in sources if (s.get("name") in curated_names) or (isinstance(s.get("config_json"), dict) and s.get("config_json", {}).get("curated")) or ("hard" in str(s.get("category") or "").lower()) or ("scene" in str(s.get("category") or "").lower())]
    if not scene_sources:
        # fallback: use all
        scene_sources = sources
    results = []
    total_items = 0
    for s in scene_sources[:max_sources]:
        enabled = s.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("1", "true", "yes")
        if not enabled:
            continue
        try:
            res = await fetch_and_import_source(s)
            results.append(res)
            total_items += int(res.get("stored_items") or 0)
        except Exception as e:
            logger.exception("scene import failed for %s", s.get("name"))
            results.append({"source": s.get("name"), "error": str(e), "stored_items": 0})
    await write_log("INFO", "scene.news_imported", f"Scene news import completed: {total_items} items", {"total_items": total_items, "sources": len(results)})
    return {"generated_at": _utc_now(), "sources_processed": len(results), "total_items_stored": total_items, "results": results}
