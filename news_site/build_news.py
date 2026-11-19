#!/usr/bin/env python3
"""Build a static news homepage and six-month archive from public RSS/Atom feeds.

The site publishes titles, short snippets, source names, timestamps, and links
back to originals. It intentionally avoids republishing full articles.
"""
from __future__ import annotations

import concurrent.futures
import email.utils
import hashlib
import html
import json
import re
import socket
import ssl
import sys
import time
import random
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

TRANSLATION_WORKERS = 3
ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "news_site" / "sources.json"
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
NEWS_DIR = PUBLIC_DIR / "news"
RU_DIR = PUBLIC_DIR / "runews"
ARCHIVE_DIR = PUBLIC_DIR / "archive"
DATA_ARCHIVE_DIR = DATA_DIR / "archive"
OUTPUT_HTML = PUBLIC_DIR / "index.html"
OUTPUT_NEWS_HTML = NEWS_DIR / "index.html"
OUTPUT_RU_HTML = RU_DIR / "index.html"
OUTPUT_ARCHIVE_HTML = ARCHIVE_DIR / "index.html"
OUTPUT_JSON = DATA_DIR / "news.json"
TRANSLATION_CACHE_PATH = DATA_DIR / "translations.zh-CN.json"
FAVICON_PATH = PUBLIC_DIR / "favicon.svg"
MAX_ITEMS_PER_SOURCE = 80
MAX_TOTAL_ITEMS = 500
ARCHIVE_RETENTION_DAYS = 183
TIMEOUT_SECONDS = 18

socket.setdefaulttimeout(TIMEOUT_SECONDS)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

FALLBACK_FAVICON_SVG = '<svg width="64" height="64" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><rect x="7" y="6" width="50" height="50" rx="13.5" fill="#F8FAFC" stroke="#CBD5E1"/><path d="M16.5 35.6C24 45.6 40 45.6 47.5 35.6M16.5 28.4C24 18.4 40 18.4 47.5 28.4" stroke="#2563EB" stroke-opacity=".42" stroke-width="2.05" stroke-linecap="round"/><circle cx="30.5" cy="30.5" r="3.55" fill="#F8FAFC" stroke="#111827" stroke-width=".85"/><path d="M29.15 30.5H31.85M30.5 29.15V31.85M33.15 33.15L36.55 36.55" stroke="#111827" stroke-width=".8" stroke-linecap="round"/></svg>'


@dataclass
class NewsItem:
    id: str
    title: str
    link: str
    source: str
    category: str
    published: str
    summary: str
    origin: str = ""
    title_zh: str = ""
    summary_zh: str = ""


def clean_text(value: str | None, limit: int | None = None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", value).strip()
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    value = clean_text(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return datetime.now(timezone.utc)


def child_text(node: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text
    wanted = {n.split("}")[-1].split(":")[-1] for n in names}
    for child in list(node):
        local = child.tag.split("}")[-1].split(":")[-1]
        if local in wanted and child.text:
            return child.text
    return ""


def child_attr(node: ET.Element, tag_local: str, attr: str) -> str:
    for child in list(node):
        local = child.tag.split("}")[-1].split(":")[-1]
        if local == tag_local and child.attrib.get(attr):
            return child.attrib[attr]
    return ""


def fetch(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AI-Agent-Space-NewsBot/1.0 (+https://aiagentspace.cc)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read(2_500_000)
    except Exception as first_exc:
        if url.startswith("https://"):
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ctx) as resp:
                return resp.read(2_500_000)
        raise first_exc


def fetch_article_rubrics(link: str) -> list[str] | None:
    """Return the article's rubric titles, or None when the page is unavailable.

    Some outlets publish advertorial content without any ad marker in the RSS
    feed, but the article page still exposes its rubric metadata. That lets us
    drop a whole advertorial section (for example RG's lifestyle pieces)
    without touching real news that shares a parent section.
    """
    req = urllib.request.Request(
        link,
        headers={
            "User-Agent": "Mozilla/5.0 AI-Agent-Space-NewsBot/1.0 (+https://aiagentspace.cc)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            page = resp.read(1_500_000).decode("utf-8", "ignore")
    except Exception:
        return None
    match = re.search(r'"rubrics"\s*:\s*\[(.*?)\]', page, re.S)
    if not match:
        return None
    return [html.unescape(title) for title in re.findall(r'"title"\s*:\s*"([^"]+)"', match.group(1))]


def parse_feed(xml_bytes: bytes, source: dict) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    feed_items: list[ET.Element] = []
    for node in root.iter():
        local = node.tag.split("}")[-1].split(":")[-1]
        if local in {"item", "entry"}:
            feed_items.append(node)

    parsed: list[NewsItem] = []
    limit = int(source.get("limit") or MAX_ITEMS_PER_SOURCE)
    for item in feed_items[:limit]:
        title = clean_text(child_text(item, ["title"]), 180)
        if not title:
            continue
        link = clean_text(child_text(item, ["link"])) or child_attr(item, "link", "href")
        summary = clean_text(child_text(item, ["description", "summary", "content", "encoded", "content:encoded"]), 300)
        published_dt = parse_datetime(child_text(item, ["pubDate", "published", "updated", "dc:date"]))
        digest = hashlib.sha1(f"{source['name']}|{link}|{title}".encode("utf-8")).hexdigest()[:16]
        origin = child_attr(item, "source", "url") or link
        parsed.append(NewsItem(digest, title, link, source["name"], source.get("category", "News"), published_dt.isoformat(), summary, origin=origin))
    return parsed


def collect() -> tuple[list[NewsItem], list[str]]:
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    items: list[NewsItem] = []
    errors: list[str] = []
    seen: set[str] = set()
    for source in sources:
        try:
            feed = fetch(source["url"])
            parsed = parse_feed(feed, source)
            excluded_rubrics = set(source.get("exclude_rubrics") or [])
            if excluded_rubrics and parsed:
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    rubrics = list(pool.map(fetch_article_rubrics, [item.link for item in parsed]))
                kept: list[NewsItem] = []
                dropped = 0
                for item, item_rubrics in zip(parsed, rubrics):
                    if item_rubrics and excluded_rubrics.intersection(item_rubrics):
                        dropped += 1
                        continue
                    kept.append(item)
                if dropped:
                    print(f"{source['name']}: filtered {dropped} advertorial item(s)", file=sys.stderr)
                parsed = kept
            for item in parsed:
                if item.id in seen or item.link in seen:
                    continue
                seen.add(item.id)
                seen.add(item.link)
                items.append(item)
        except Exception as exc:
            errors.append(f"{source.get('name', source.get('url'))}: {exc}")
    items.sort(key=lambda n: n.published, reverse=True)
    return items[:MAX_TOTAL_ITEMS], errors


def load_translation_cache() -> dict[str, str]:
    if not TRANSLATION_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_translation_cache(cache: dict[str, str]) -> None:
    TRANSLATION_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def looks_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value or ""))


def translation_key(value: str) -> str:
    return hashlib.sha1(clean_text(value, 480).encode("utf-8")).hexdigest()


def translate_batch(values: list[str]) -> list[str]:
    """Translate a batch of strings with the Google translate web endpoint.

    The per-item gtx endpoint answers 429 aggressively from server IPs, while
    the clients5 batch endpoint tolerates small, paced batches. Values are sent
    as repeated q parameters; the response keeps the same order.
    """
    query = [("client", "dict-chrome-ex"), ("sl", "auto"), ("tl", "zh-CN")]
    query.extend(("q", value) for value in values)
    url = "https://clients5.google.com/translate_a/t?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 AI-Agent-Space-NewsBot/1.0 (+https://aiagentspace.cc)"},
    )
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            time.sleep(random.uniform(0.2, 0.45))
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read(1_500_000).decode("utf-8"))
            translated: list[str] = []
            for segment in payload:
                if isinstance(segment, list) and segment and isinstance(segment[0], str):
                    translated.append(segment[0])
                elif isinstance(segment, str):
                    translated.append(segment)
                else:
                    translated.append("")
            if len(translated) != len(values):
                raise RuntimeError(f"unexpected batch response size {len(translated)} for {len(values)} inputs")
            return [clean_text(item, 480) or original for item, original in zip(translated, values)]
        except urllib.error.HTTPError as exc:
            last_exc = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
            else:
                delay = min(30.0, 2.5 * (attempt + 1) + random.uniform(0.0, 1.0))
            time.sleep(delay)
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5 * (attempt + 1))
    raise last_exc or RuntimeError("translation failed")


def translate_text(value: str) -> str:
    value = clean_text(value, 480)
    if not value or looks_chinese(value):
        return value
    return translate_batch([value])[0]


def translation_chunks(values: list[str], max_query_bytes: int = 5500) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for value in values:
        encoded_size = len(urllib.parse.quote(value, safe="")) + 8
        if current and size + encoded_size > max_query_bytes:
            chunks.append(current)
            current = []
            size = 0
        current.append(value)
        size += encoded_size
    if current:
        chunks.append(current)
    return chunks


def enrich_translations(items: list[NewsItem]) -> None:
    cache = load_translation_cache()
    unique_texts: dict[str, str] = {}
    for item in items:
        for value in (item.title, item.summary):
            value = clean_text(value, 480)
            if not value or looks_chinese(value):
                continue
            key = translation_key(value)
            if key not in cache:
                unique_texts[key] = value

    if unique_texts:
        value_to_key = {value: key for key, value in unique_texts.items()}
        batches = translation_chunks(list(value_to_key))
        print(
            f"Translating {len(value_to_key)} uncached text snippets in {len(batches)} batches with {TRANSLATION_WORKERS} workers",
            file=sys.stderr,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=TRANSLATION_WORKERS) as executor:
            future_to_batch = {executor.submit(translate_batch, batch): batch for batch in batches}
            for future in concurrent.futures.as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    for value, translated in zip(batch, future.result()):
                        # Do not cache an echo of the original (usually a transient
                        # endpoint hiccup); it is retried on the next run instead.
                        if translated and translated != value:
                            cache[value_to_key[value]] = translated
                except Exception as exc:
                    # Leave the snippets uncached so a later run can retry them.
                    print(f"Translation retry later for {len(batch)} snippets: {exc}", file=sys.stderr)
        save_translation_cache(cache)

    for item in items:
        item.title_zh = item.title if looks_chinese(item.title) else cache.get(translation_key(item.title), item.title)
        item.summary_zh = item.summary if looks_chinese(item.summary) else cache.get(translation_key(item.summary), item.summary)


def display_time(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_value


def category_class(category: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")
    return normalized or "news"


def item_day(item: NewsItem) -> str:
    try:
        return datetime.fromisoformat(item.published).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def archive_cutoff_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=ARCHIVE_RETENTION_DAYS)).strftime("%Y-%m-%d")


def prune_archives(cutoff: str | None = None) -> None:
    cutoff = cutoff or archive_cutoff_day()
    for directory in (DATA_ARCHIVE_DIR, ARCHIVE_DIR):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            if path.stem < cutoff:
                path.unlink(missing_ok=True)
        for path in directory.glob("*.html"):
            if path.name != "index.html" and path.stem < cutoff:
                path.unlink(missing_ok=True)


def merge_daily_archives(items: list[NewsItem]) -> None:
    DATA_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item_day(item), []).append(asdict(item))

    cutoff = archive_cutoff_day()
    for day, day_items in grouped.items():
        if day < cutoff:
            continue
        path = DATA_ARCHIVE_DIR / f"{day}.json"
        existing: dict[str, dict] = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for old in payload.get("items", []):
                    key = old.get("id") or old.get("link")
                    if key:
                        existing[key] = old
            except Exception:
                existing = {}
        for item in day_items:
            key = item.get("id") or item.get("link")
            if key:
                existing[key] = item
        merged = sorted(existing.values(), key=lambda n: n.get("published", ""), reverse=True)
        payload = {"date": day, "generated_at": datetime.now(timezone.utc).isoformat(), "count": len(merged), "items": merged}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    prune_archives(cutoff)


def load_archive_payloads() -> list[dict]:
    if not DATA_ARCHIVE_DIR.exists():
        return []
    cutoff = archive_cutoff_day()
    payloads: list[dict] = []
    for path in sorted(DATA_ARCHIVE_DIR.glob("*.json"), reverse=True):
        if path.stem < cutoff:
            continue
        try:
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return payloads


def item_from_dict(raw: dict) -> NewsItem:
    data = {field: raw.get(field, "") for field in NewsItem.__dataclass_fields__}
    return NewsItem(**data)


def favicon_svg() -> str:
    try:
        return FAVICON_PATH.read_text(encoding="utf-8")
    except Exception:
        return FALLBACK_FAVICON_SVG


def news_css() -> str:
    return """
    :root { --ink: #111827; --body: #4d4d4d; --muted: #888; --hairline: #ebebeb; --canvas: #fff; --soft: #fafafa; --link: #0070f3; font-family: Geist, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    * { box-sizing: border-box; } body { margin: 0; color: var(--ink); background: var(--canvas); } a { color: inherit; text-decoration: none; }
    .shell { max-width: 1180px; margin: 0 auto; padding: 0 24px; } header { position: sticky; top: 0; z-index: 20; backdrop-filter: saturate(180%) blur(18px); background: rgba(255,255,255,.82); border-bottom: 1px solid var(--hairline); }
    .nav { height: 64px; display: flex; align-items: center; justify-content: space-between; gap: 24px; } .brand { display: inline-flex; align-items: center; justify-content: center; width: 40px; height: 40px; border-radius: 12px; }
    .mark { width: 34px; height: 34px; display: block; filter: drop-shadow(0 7px 14px rgba(15,23,42,.14)) drop-shadow(0 1px 2px rgba(15,23,42,.10)); } .mark .mark-bg { fill: #f8fafc; } .mark .mark-border { fill: none; stroke: #cbd5e1; stroke-width: .88; } .mark .mark-shine { fill: #fff; opacity: .42; } .mark .mark-glint { fill: none; stroke: #fff; stroke-opacity: .82; stroke-width: .72; stroke-linecap: round; } .mark .mark-orbit { fill: none; stroke: rgba(37,99,235,.42); stroke-width: 1.08; stroke-linecap: round; } .mark .mark-lens { fill: #f8fafc; stroke: #111827; stroke-width: .5; } .mark .mark-plus { fill: none; stroke: #111827; stroke-width: .38; stroke-linecap: round; } .mark .mark-handle { fill: none; stroke: #111827; stroke-width: .54; stroke-linecap: round; }
    .nav-meta { display: flex; align-items: center; gap: 14px; color: var(--body); font-size: 13px; } .nav-meta a { border: 1px solid var(--hairline); padding: 7px 11px; border-radius: 999px; background: var(--canvas); }
    .hero { position: relative; overflow: hidden; border-bottom: 1px solid var(--hairline); } .hero::before { content: ''; position: absolute; inset: -30% -15% auto; height: 420px; opacity: .22; background: radial-gradient(circle at 25% 25%, #007cf0, transparent 26%), radial-gradient(circle at 50% 20%, #00dfd8, transparent 24%), radial-gradient(circle at 72% 28%, #ff0080, transparent 26%), radial-gradient(circle at 60% 58%, #f9cb28, transparent 18%); filter: blur(18px); }
    .hero-inner { position: relative; padding: 86px 0 72px; display: grid; grid-template-columns: 1.08fr .92fr; gap: 56px; align-items: end; } .eyebrow { display: inline-flex; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--body); border: 1px solid var(--hairline); border-radius: 999px; padding: 7px 10px; background: rgba(255,255,255,.72); }
    h1 { font-size: clamp(48px, 8vw, 92px); line-height: .92; letter-spacing: -0.07em; margin: 22px 0; max-width: 760px; } .hero p { color: var(--body); font-size: 18px; line-height: 1.65; max-width: 660px; margin: 0; }
    .hero-card { border: 1px solid var(--hairline); background: rgba(255,255,255,.84); border-radius: 24px; padding: 24px; box-shadow: 0 24px 80px rgba(0,0,0,.08); } .hero-card .source { font-family: ui-monospace, monospace; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; } .hero-card h2 { font-size: 28px; line-height: 1.12; letter-spacing: -.04em; margin: 14px 0 12px; } .hero-card p { font-size: 15px; line-height: 1.55; } .more { display: inline-flex; margin-top: 20px; color: #fff; background: var(--ink); border-radius: 999px; padding: 10px 14px; font-weight: 520; }
    .toolbar { padding: 24px 0; border-bottom: 1px solid var(--hairline); background: var(--soft); } .toolbar-row { display: flex; justify-content: space-between; gap: 18px; align-items: center; flex-wrap: wrap; } .tabs, .source-filters { display: flex; gap: 8px; flex-wrap: wrap; } .category-filter, .stat, .source-filter { border: 1px solid var(--hairline); background: var(--canvas); border-radius: 999px; padding: 8px 12px; font-size: 13px; color: var(--body); } .source-panel { padding-top: 16px; } .source-panel h2 { margin: 0 0 12px; font-size: 14px; color: var(--body); } .source-filter, .category-filter { cursor: pointer; font: inherit; } .source-filter.active, .category-filter.active { background: var(--ink); border-color: var(--ink); color: white; }
    .grid { padding: 42px 0 80px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; } .news-card { min-height: 260px; border: 1px solid var(--hairline); border-radius: 20px; padding: 20px; background: var(--canvas); transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; } .news-card:hover { transform: translateY(-2px); border-color: #cfcfcf; box-shadow: 0 18px 60px rgba(0,0,0,.06); } .news-card.is-hidden { display: none; }
    .card-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: var(--muted); font-size: 12px; line-height: 1.4; } .pill { color: var(--ink); border: 1px solid var(--hairline); border-radius: 999px; padding: 3px 8px; background: var(--soft); } .pill-technology { background: #e9fbff; border-color: #c9f2fb; } .pill-business { background: #fff7e6; border-color: #ffe2a8; } .pill-world { background: #f1edff; border-color: #ddd1ff; } .pill-russia { background: #eef4ff; border-color: #cfe0ff; }
    .news-card h3 { margin: 18px 0 12px; font-size: 21px; line-height: 1.18; letter-spacing: -.045em; } .news-card h3 a:hover { color: var(--link); } .news-card p { color: var(--body); font-size: 14px; line-height: 1.58; margin: 0; } .original { margin-top: 14px; border-top: 1px solid var(--hairline); padding-top: 12px; color: var(--muted); } .original summary { cursor: pointer; font-size: 12px; } .original h4 { margin: 10px 0 6px; font-size: 14px; line-height: 1.35; color: var(--body); } .original p { font-size: 13px; color: var(--muted); }
    .archive-list { padding: 42px 0 80px; display: grid; gap: 12px; } .archive-row { border: 1px solid var(--hairline); border-radius: 18px; padding: 18px 20px; background: var(--canvas); display: flex; justify-content: space-between; gap: 16px; align-items: center; } .archive-row:hover { border-color: #cfcfcf; box-shadow: 0 14px 46px rgba(0,0,0,.045); } .archive-row strong { font-size: 18px; letter-spacing: -.03em; } .archive-row span { color: var(--muted); font-size: 13px; }
    .errors { margin: -48px auto 56px; max-width: 1180px; padding: 0 24px; color: var(--muted); font-size: 13px; } footer { border-top: 1px solid var(--hairline); background: var(--soft); color: var(--muted); font-size: 13px; } footer .shell { padding-top: 28px; padding-bottom: 28px; display: flex; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
    @media (max-width: 860px) { .hero-inner { grid-template-columns: 1fr; padding: 58px 0 48px; } .grid { grid-template-columns: 1fr; } .nav-meta span { display: none; } }
    """


def mark_svg() -> str:
    return '<svg class="mark" viewBox="0 0 32 32" role="img" aria-hidden="true" xmlns="http://www.w3.org/2000/svg"><rect class="mark-bg" x="3" y="3" width="26" height="26" rx="7.2"/><rect class="mark-border" x="3.35" y="3.35" width="25.3" height="25.3" rx="6.85"/><rect class="mark-shine" x="5" y="5" width="22" height="22" rx="6"/><path class="mark-orbit" d="M8.2 17.8c3.7 5.1 11.8 5.1 15.5 0M8.2 14.2c3.7-5.1 11.8-5.1 15.5 0"/><circle class="mark-lens" cx="15.3" cy="15.3" r="1.9"/><path class="mark-plus" d="M14.5 15.3H16.1M15.3 14.5V16.1"/><path class="mark-handle" d="M16.75 16.75L18.45 18.45"/><path class="mark-glint" d="M7.2 7.2C9.7 5.8 12.8 5.2 16 5.2"/></svg>'


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_ru_item(item: NewsItem) -> bool:
    """True when the story was published on a .ru site.

    Google News items link to news.google.com, so the publisher host from the
    feed's <source url="..."> element is used when available.
    """
    host = host_of(item.origin) or host_of(item.link)
    return host == "ru" or host.endswith(".ru")


def nav_html() -> str:
    return f'<header><div class="shell nav"><a class="brand" href="/" aria-label="AI Agent Space News home">{mark_svg()}</a><div class="nav-meta"><span>Updated every 2 hours</span><a href="/news/">News</a><a href="/runews/">RU News</a><a href="/archive/">Archive</a><a href="/news.json">Data feed</a></div></div></header>'


def render(items: list[NewsItem], errors: list[str], page_label: str | None = None, hero_heading: str = "全球新闻观察", page_title: str = "AI Agent Space News") -> str:
    now = datetime.now(timezone.utc)
    top = items[0] if items else None
    categories = sorted({item.category for item in items})
    cards = []
    for item in items:
        title_zh = html.escape(item.title_zh or item.title)
        summary_zh = html.escape(item.summary_zh or item.summary or "打开原文查看详情。")
        title_en = html.escape(item.title)
        summary_en = html.escape(item.summary or "Open the original source for details.")
        cards.append(f'''
        <article class="news-card" data-category="{html.escape(item.category)}" data-source="{html.escape(item.source)}" data-source-slug="{category_class(item.source)}">
          <div class="card-meta"><span class="pill pill-{category_class(item.category)}">{html.escape(item.category)}</span><span>{html.escape(item.source)}</span><span>{display_time(item.published)}</span></div>
          <h3><a href="{html.escape(item.link)}" target="_blank" rel="noopener noreferrer">{title_zh}</a></h3>
          <p>{summary_zh}</p>
          <details class="original"><summary>原文</summary><h4>{title_en}</h4><p>{summary_en}</p></details>
        </article>''')
    category_tabs = '<button class="category-filter active" data-category="all">全部</button>' + "".join(f'<button class="category-filter" data-category="{html.escape(cat)}">{html.escape(cat)}</button>' for cat in categories)
    sources = sorted({item.source for item in items})
    source_filters = '<button class="source-filter active" data-source="all">全部</button>' + "".join(f'<button class="source-filter" data-source="{html.escape(source)}">{html.escape(source)}</button>' for source in sources)
    error_block = ""
    if errors:
        error_block = "<details class='errors'><summary>Feed notes</summary><ul>" + "".join(f"<li>{html.escape(err)}</li>" for err in errors) + "</ul></details>"
    hero_title = html.escape((top.title_zh or top.title) if top else "AI Agent Space News")
    hero_summary = html.escape((top.summary_zh or top.summary) if top else "暂时还没有抓取到新闻。")
    hero_link = html.escape(top.link if top else "#")
    hero_source = html.escape(top.source if top else "System")
    label = page_label or f"LIVE DIGEST · {now.strftime('%Y-%m-%d %H:%M UTC')}"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{page_title}</title><meta name="description" content="每 2 小时自动追踪指定国际新闻源，展示中文标题、中文摘要和原文链接。" /><link rel="canonical" href="/news/" /><link rel="icon" href="/favicon.svg" type="image/svg+xml" /><style>{news_css()}</style></head><body>{nav_html()}<main><section class="hero"><div class="shell hero-inner"><div><div class="eyebrow">{label}</div><h1>{hero_heading}</h1></div><article class="hero-card"><div class="source">Top story · {hero_source}</div><h2><a href="{hero_link}" target="_blank" rel="noopener noreferrer">{hero_title}</a></h2><p>{hero_summary}</p><a class="more" href="{hero_link}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></article></div></section><section class="toolbar"><div class="shell toolbar-row"><div class="tabs">{category_tabs}</div><div class="stat"><span id="visible-count">{len(items)}</span> / {len(items)} 条新闻 · 中文摘要 · 链接到原文</div></div><div class="shell source-panel"><h2>按来源筛选</h2><div class="source-filters" role="list" aria-label="Filter by source">{source_filters}</div></div></section><section class="shell grid">{''.join(cards)}</section>{error_block}</main><footer><div class="shell"><span>© {now.year} AI Agent Space News</span><span>中文标题和摘要由自动翻译生成；完整报道与版权归原媒体所有。</span></div></footer><script>(() => {{ const sourceButtons = [...document.querySelectorAll('.source-filter')]; const categoryButtons = [...document.querySelectorAll('.category-filter')]; const cards = [...document.querySelectorAll('.news-card')]; const counter = document.querySelector('#visible-count'); const state = {{ source: 'all', category: 'all' }}; const apply = () => {{ let visible = 0; for (const card of cards) {{ const sourceOk = state.source === 'all' || card.dataset.source === state.source; const categoryOk = state.category === 'all' || card.dataset.category === state.category; const show = sourceOk && categoryOk; card.classList.toggle('is-hidden', !show); if (show) visible += 1; }} if (counter) counter.textContent = visible; for (const button of sourceButtons) button.classList.toggle('active', button.dataset.source === state.source); for (const button of categoryButtons) button.classList.toggle('active', button.dataset.category === state.category); }}; for (const button of sourceButtons) button.addEventListener('click', () => {{ state.source = button.dataset.source; apply(); }}); for (const button of categoryButtons) button.addEventListener('click', () => {{ state.category = button.dataset.category; apply(); }}); }})();</script></body></html>'''



def render_home(items: list[NewsItem], errors: list[str]) -> str:
    now = datetime.now(timezone.utc)
    sources = sorted({item.source for item in items})
    categories = sorted({item.category for item in items})
    source_list = "".join(f"<span>{html.escape(source)}</span>" for source in sources)
    category_list = "".join(f"<span>{html.escape(cat)}</span>" for cat in categories)
    error_note = ""
    if errors:
        error_note = f"<p class='home-note'>本次更新有 {len(errors)} 个源暂时不可用；新闻页会继续使用可用源更新。</p>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Agent Space</title>
  <meta name="description" content="AI Agent Space 的入口页。新闻聚合在 /news/，每日归档在 /archive/。" />
  <link rel="canonical" href="/" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <style>{news_css()}
    .home-actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 26px; }}
    .home-action {{ display: inline-flex; align-items: center; border: 1px solid var(--ink); background: var(--ink); color: #fff; border-radius: 999px; padding: 12px 16px; font-weight: 560; }}
    .home-action.secondary {{ background: var(--canvas); color: var(--ink); border-color: var(--hairline); }}
    .home-section {{ padding: 42px 0 80px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }}
    .home-card {{ border: 1px solid var(--hairline); border-radius: 22px; padding: 22px; background: var(--canvas); min-height: 190px; }}
    .home-card h2 {{ margin: 0 0 12px; font-size: 22px; letter-spacing: -.04em; }}
    .home-card p, .home-note {{ color: var(--body); line-height: 1.65; margin: 0; }}
    .home-tags {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }}
    .home-tags span {{ border: 1px solid var(--hairline); border-radius: 999px; padding: 6px 10px; color: var(--body); font-size: 12px; background: var(--soft); }}
    @media (max-width: 860px) {{ .home-section {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  {nav_html()}
  <main>
    <section class="hero">
      <div class="shell hero-inner">
        <div>
          <div class="eyebrow">HOME · {now.strftime('%Y-%m-%d %H:%M UTC')}</div>
          <h1>AI Agent Space</h1>
          <p>这是站点首页。新闻聚合内容已移到独立的 News 页面，首页只作为导航入口和项目说明。</p>
          <div class="home-actions">
            <a class="home-action" href="/news/">进入新闻页 ↗</a>
            <a class="home-action secondary" href="/archive/">查看每日归档</a>
            <a class="home-action secondary" href="/news.json">打开数据源</a>
          </div>
        </div>
        <article class="hero-card">
          <div class="source">Status</div>
          <h2>每 2 小时更新</h2>
          <p>当前新闻页保留最新 {len(items)} 条；每日归档最多保存最近 {ARCHIVE_RETENTION_DAYS} 天。</p>
        </article>
      </div>
    </section>
    <section class="shell home-section">
      <article class="home-card"><h2>News</h2><p>新闻页展示中文标题、中文摘要、来源筛选和原文展开。</p><div class="home-actions"><a class="home-action secondary" href="/news/">/news/</a></div></article>
      <article class="home-card"><h2>Archive</h2><p>归档页按日期保存每日新闻快照，自动清理半年以前的归档。</p><div class="home-actions"><a class="home-action secondary" href="/archive/">/archive/</a></div></article>
      <article class="home-card"><h2>Sources</h2><p>追踪公开 RSS 和 Google News site feed，只展示标题、摘要和原文链接。</p><div class="home-tags">{source_list}</div></article>
    </section>
    <section class="shell" style="padding-bottom:72px;"><div class="home-card"><h2>Categories</h2><p>当前跟踪的新闻分类：</p><div class="home-tags">{category_list}</div>{error_note}</div></section>
  </main>
  <footer><div class="shell"><span>© {now.year} AI Agent Space</span><span>首页不展示新闻内容；新闻请访问 /news/。</span></div></footer>
</body>
</html>"""

def render_archive_index() -> str:
    payloads = load_archive_payloads()
    now = datetime.now(timezone.utc)
    rows: list[str] = []
    total = 0
    for payload in payloads:
        day = payload.get("date", "")
        count = int(payload.get("count") or len(payload.get("items", [])))
        total += count
        first = payload.get("items", [{}])[0] if payload.get("items") else {}
        sample = html.escape(first.get("title_zh") or first.get("title") or "查看当日新闻")
        rows.append(f'<a class="archive-row" href="/archive/{html.escape(day)}.html"><div><strong>{html.escape(day)}</strong><br><span>{sample}</span></div><span>{count} 条</span></a>')
    rows_html = "".join(rows) or "<p>暂无归档。下一次自动更新后会生成每日归档。</p>"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>新闻归档 · AI Agent Space News</title><meta name="description" content="AI Agent Space News 最近半年的每日新闻归档。" /><link rel="canonical" href="/archive/" /><link rel="icon" href="/favicon.svg" type="image/svg+xml" /><style>{news_css()}</style></head><body>{nav_html()}<main><section class="hero"><div class="shell hero-inner"><div><div class="eyebrow">ARCHIVE · {now.strftime('%Y-%m-%d %H:%M UTC')}</div><h1>每日新闻归档。</h1><p>这里保存最近半年的每日新闻快照。每 2 小时自动更新新闻页，并把当天抓到的新闻合并进对应日期归档。</p></div><article class="hero-card"><div class="source">Archive</div><h2>{len(payloads)} 天 · {total} 条</h2><p>最多保留最近 {ARCHIVE_RETENTION_DAYS} 天，超过半年的 JSON 与 HTML 归档会在生成时自动清理。</p><a class="more" href="/news/">返回新闻页 ↗</a></article></div></section><section class="shell archive-list">{rows_html}</section></main><footer><div class="shell"><span>© {now.year} AI Agent Space News</span><span>归档仅保存标题、摘要、来源、时间与原文链接。</span></div></footer></body></html>'''


def write_archive_pages() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for payload in load_archive_payloads():
        day = payload.get("date")
        if not day:
            continue
        items = [item_from_dict(raw) for raw in payload.get("items", [])]
        (ARCHIVE_DIR / f"{day}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (ARCHIVE_DIR / f"{day}.html").write_text(render(items, [], page_label=f"ARCHIVE · {day}"), encoding="utf-8")
    OUTPUT_ARCHIVE_HTML.write_text(render_archive_index(), encoding="utf-8")


def main() -> int:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    items, errors = collect()
    enrich_translations(items)
    merge_daily_archives(items)

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "count": len(items), "items": [asdict(item) for item in items], "errors": errors}
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (PUBLIC_DIR / "news.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_output = render(items, errors)
    OUTPUT_HTML.write_text(render_home(items, errors), encoding="utf-8")
    OUTPUT_NEWS_HTML.write_text(html_output, encoding="utf-8")
    ru_items = [item for item in items if is_ru_item(item)]
    RU_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_RU_HTML.write_text(
        render(ru_items, errors, page_label=f"RU NEWS · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", hero_heading="俄语新闻", page_title="RU News · AI Agent Space"),
        encoding="utf-8",
    )
    write_archive_pages()
    FAVICON_PATH.write_text(favicon_svg(), encoding="utf-8")
    print(f"Generated {OUTPUT_NEWS_HTML} with {len(items)} stories")
    print(f"Generated {OUTPUT_RU_HTML} with {len(ru_items)} .ru stories")
    print(f"Generated {OUTPUT_ARCHIVE_HTML} with {len(load_archive_payloads())} archive days")
    if errors:
        print("Feed errors:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
