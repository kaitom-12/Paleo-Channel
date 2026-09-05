#!/usr/bin/env python3
"""Export D-Lab Paleo articles into structure-preserving Markdown.

Verified authenticated HTML is preferred when available. The Algolia search
index remains the fallback, and the exporter never invents missing content.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from lxml import html as lxml_html
except ImportError:  # pragma: no cover - stdlib fallback remains available
    lxml_html = None


APP_ID = "PD3UJWKGZD"
API_KEY = (
    "OWY2YzIxOWE3ZTg5MjFiODQyOGEyM2UxNTllMTNjZjM1MmRhZTJhZGZmZWY2NzVh"
    "NTViNDRjYmVjYTM4ZTFlZGF0dHJpYnV0ZXNUb1JldHJpZXZlPSU1QiUyMiolMjIl"
    "MkMlMjItc2VjcmV0JTIyJTVE"
)
INDEX = "blogs_regist_time_desc"
BASE_URL = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{INDEX}/query"
JST = ZoneInfo("Asia/Tokyo")

DLAB = "D\u30e9\u30dc"
PALEO = "\u30d1\u30ec\u30aa"
ARTICLE = "\u8a18\u4e8b"
SOURCE_TYPE_DLAB = "D\u30e9\u30dc\u5185\u8a18\u4e8b"
SOURCE_TYPE_BLOG = "\u30d1\u30ec\u30aa\u306a\u7537\u30d6\u30ed\u30b0\u7531\u6765"
CONTENT_SOURCE_INDEX = "D\u30e9\u30dc\u691c\u7d22\u7d22\u5f15"
CONTENT_SOURCE_AUTHENTICATED = "D\u30e9\u30dc\u8a8d\u8a3c\u6e08\u307f\u672c\u6587"

COLLECTION_STATUS_FULL = "full"
COLLECTION_STATUS_ASSET_FAILED = "asset_failed"
COLLECTION_STATUS_PARTIAL = "partial"
COLLECTION_STATUS_PENDING = "pending"
COLLECTION_STATUS_FAILED = "failed"
ASSET_STATUS_NONE = "none"
ASSET_STATUS_COMPLETE = "complete"
ASSET_STATUS_PARTIAL = "partial"


@dataclass
class Article:
    object_id: str
    title: str
    body: str
    regist_ms: int
    channel_owner: str
    origin: str | None
    thumbnail_url: str | None


def algolia_query(params: str) -> dict:
    payload = json.dumps({"params": params}).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Algolia-Application-Id": APP_ID,
            "X-Algolia-API-Key": API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query_page(page: int, hits_per_page: int, extra_filter: str | None = None) -> dict:
    filters = "channel_owner:paleo"
    if extra_filter:
        filters = f"{filters} AND {extra_filter}"
    params = {
        "query": "",
        "hitsPerPage": str(hits_per_page),
        "page": str(page),
        "filters": filters,
        "attributesToRetrieve": json.dumps(
            [
                "title",
                "body",
                "blog_regist_time",
                "channel_owner",
                "origin",
                "thumbnail_url",
                "objectID",
            ],
            ensure_ascii=False,
        ),
    }
    return algolia_query(urllib.parse.urlencode(params))


def fetch_recent(limit: int) -> list[Article]:
    out: list[Article] = []
    page = 0
    hits_per_page = min(100, max(1, limit))
    while len(out) < limit:
        data = query_page(page, hits_per_page)
        hits = data.get("hits", [])
        if not hits:
            break
        out.extend(article_from_hit(hit) for hit in hits)
        page += 1
    return dedupe(out)[:limit]


def fetch_count(extra_filter: str | None = None) -> int:
    filters = "channel_owner:paleo"
    if extra_filter:
        filters = f"{filters} AND {extra_filter}"
    params = {
        "query": "",
        "hitsPerPage": "1",
        "page": "0",
        "filters": filters,
        "attributesToRetrieve": json.dumps(["objectID"]),
    }
    return int(algolia_query(urllib.parse.urlencode(params)).get("nbHits", 0))


def fetch_all(max_records: int | None = None) -> list[Article]:
    latest = query_page(0, 1).get("hits", [])
    high_ms = int(latest[0]["blog_regist_time"]) + 1 if latest else int(time.time() * 1000)
    records: list[Article] = []
    seen: set[str] = set()
    ranges = [(0, high_ms)]
    while ranges:
        lo, hi = ranges.pop()
        count = fetch_count(f"blog_regist_time >= {lo} AND blog_regist_time < {hi}")
        if count == 0:
            continue
        # Keep every page range below Algolia's practical pagination limit.
        # The previous implementation did not split a dense sub-day range,
        # which could silently omit records when it exceeded 1,000 hits.
        if count > 900:
            if hi - lo <= 1:
                raise RuntimeError(
                    f"cannot safely split dense timestamp range: {lo}-{hi} ({count} hits)"
                )
            mid = (lo + hi) // 2
            ranges.append((lo, mid))
            ranges.append((mid, hi))
            continue
        pages = (count + 99) // 100
        range_records = 0
        for page in range(pages):
            data = query_page(page, 100, f"blog_regist_time >= {lo} AND blog_regist_time < {hi}")
            hits = data.get("hits", [])
            expected_page_hits = min(100, count - page * 100)
            if len(hits) != expected_page_hits:
                raise RuntimeError(
                    "Algolia page count mismatch for "
                    f"{lo}-{hi}: page={page} expected={expected_page_hits} got={len(hits)}"
                )
            range_records += len(hits)
            for hit in hits:
                article = article_from_hit(hit)
                if not article.object_id:
                    raise RuntimeError(
                        f"Algolia returned a hit without objectID in range {lo}-{hi}"
                    )
                if article.object_id in seen:
                    raise RuntimeError(
                        f"Algolia returned a duplicate object_id: {article.object_id}"
                    )
                seen.add(article.object_id)
                records.append(article)
                if max_records and len(records) >= max_records:
                    return sorted(records, key=lambda x: x.regist_ms, reverse=True)
            time.sleep(0.02)
        if range_records != count:
            raise RuntimeError(
                f"Algolia range count mismatch for {lo}-{hi}: expected={count} got={range_records}"
            )
    return sorted(records, key=lambda x: x.regist_ms, reverse=True)


def article_from_hit(hit: dict) -> Article:
    return Article(
        object_id=str(hit.get("objectID") or ""),
        title=str(hit.get("title") or "").strip(),
        body=str(hit.get("body") or ""),
        regist_ms=int(hit.get("blog_regist_time") or hit.get("create_time") or 0),
        channel_owner=str(hit.get("channel_owner") or "paleo"),
        origin=hit.get("origin") or None,
        thumbnail_url=hit.get("thumbnail_url") or None,
    )


def dedupe(items: list[Article]) -> list[Article]:
    out: list[Article] = []
    seen: set[str] = set()
    for item in items:
        if item.object_id and item.object_id not in seen:
            seen.add(item.object_id)
            out.append(item)
    return out


def clean_body(body: str) -> str:
    text = html.unescape(body or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u00a0", "\n")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_blocks(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n", text)]
    return [b for b in blocks if b]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


HEADING_RE = re.compile(
    r"^(第\d+|\d+[\.．、]|#\d+|ステップ\d+|ワーク\d+|チェックポイント\d+|ポイント\d+|改善|判定|"
    r"まとめ|総合|実践|私の|本日の|なぜ|オメガ|カリスマ|.+[:：].{0,60}$)"
)


def is_heading(block: str) -> bool:
    s = normalize_space(block)
    if len(s) < 3 or len(s) > 80:
        return False
    if any(mark in s for mark in ["。", "→"]) or s.endswith("、") or s.startswith("タイプAI"):
        return False
    if s.count("：") + s.count(":") > 1:
        return False
    if re.search(r"(グループ|男性|女性|歳).*(グループ|男性|女性|歳)", s):
        return False
    if HEADING_RE.search(s):
        return True
    return bool(
        re.search(
            r"(考えてみよう|見てみよう|探せ|探そう|使う|作れる|守ろう|やってみよう|"
            r"チェック|トレーニング|ガイド|まとめ|方法|ポイント|問題|手順|注意点|ワーク|ベスト|おすすめ)$",
            s,
        )
    )


def strong_single_heading(heading: str) -> bool:
    return bool(
        heading
        and "：" not in heading
        and ":" not in heading
        and re.search(r"(考えてみよう|見てみよう|探せ|探そう|作れる|チェック|ガイド|まとめ|方法|ポイント)", heading)
    )


def extract_headings(text: str) -> list[str]:
    return [normalize_space(block) for block in split_blocks(text) if is_heading(block)]


def date_parts(ms: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M"), dt.strftime("%Y%m%d")


def safe_filename(date_compact: str, title: str, object_id: str, existing: set[str]) -> str:
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    base = re.sub(r"\s+", "", base)
    base = re.sub(r"[、。，．「」『』【】（）()！？!?:：#“”‘’]", "", base)
    base = base[:42] or object_id
    name = f"{date_compact}_{base}.md"
    if name in existing:
        name = f"{date_compact}_{base}_{object_id}.md"
    existing.add(name)
    return name


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def canonical_asset_url(value: str | None) -> str:
    return html.unescape(value or "").strip()


def asset_alias_url(value: str | None) -> str:
    """Normalize Firebase token variants to the same underlying object path."""
    original = canonical_asset_url(value)
    if not original:
        return ""
    try:
        parsed = urllib.parse.urlsplit(original)
    except ValueError:
        return original
    if parsed.hostname != "firebasestorage.googleapis.com":
        return original
    query = [
        (key, item)
        for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "token"
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def localize_asset_url(value: str | None, image_url_map: dict[str, str] | None) -> str:
    original = canonical_asset_url(value)
    if image_url_map and original in image_url_map:
        return image_url_map[original]
    return original


def localize_html_image_urls(markup: str, image_url_map: dict[str, str] | None) -> str:
    if not image_url_map:
        return markup
    localized = markup
    for source_url, local_file in image_url_map.items():
        localized = localized.replace(source_url, local_file)
        localized = localized.replace(html.escape(source_url, quote=True), local_file)
    return localized


def _fallback_source_body_to_markdown(
    body: str, image_url_map: dict[str, str] | None = None
) -> str:
    """Preserve the indexed source text with only light Markdown conversion."""
    text = html.unescape(body or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    def anchor_to_markdown(match: re.Match[str]) -> str:
        href = match.group(1).strip()
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip() or href
        return f"[{label}]({href})"

    def image_to_markdown(match: re.Match[str]) -> str:
        attrs = match.group(1)
        src_match = re.search(r"\bsrc=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        if not src_match:
            return ""
        alt_match = re.search(r"\balt=[\"']([^\"']*)[\"']", attrs, flags=re.I)
        alt = alt_match.group(1).strip() if alt_match else "記事内画像"
        src = localize_asset_url(src_match.group(1), image_url_map)
        return f"\n\n![{alt}]({src})\n\n" if src else ""

    text = re.sub(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        anchor_to_markdown,
        text,
        flags=re.I | re.S,
    )
    text = re.sub(r"<img\b([^>]*)>", image_to_markdown, text, flags=re.I | re.S)
    for level in range(1, 7):
        text = re.sub(
            rf"<h{level}\b[^>]*>(.*?)</h{level}>",
            lambda match, level=level: (
                f"\n\n{'#' * min(level + 1, 6)} "
                f"{re.sub(r'<[^>]+>', '', match.group(1)).strip()}\n\n"
            ),
            text,
            flags=re.I | re.S,
        )
    text = re.sub(
        r"<li\b[^>]*>(.*?)</li>",
        lambda match: f"\n- {re.sub(r'<[^>]+>', '', match.group(1)).strip()}",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(
        r"</?(?:iframe|input|select|option|textarea|video|audio|form)\b[^>]*>",
        lambda match: f"\n\n{match.group(0)}\n\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|ul|ol|blockquote)>", "\n\n", text, flags=re.I)
    text = re.sub(
        r"<(?!/?(?:iframe|input|select|option|textarea|video|audio|form)\b)[^>]+>",
        "",
        text,
    )

    rendered_blocks: list[str] = []
    current_block: list[str] = []
    blank_run = 0

    def flush_block() -> None:
        if not current_block:
            return
        rendered_blocks.append("\\\n".join(current_block))
        current_block.clear()

    for raw_line in text.split("\n"):
        line = raw_line.replace("\u00a0", " ").strip()
        if not line:
            flush_block()
            blank_run += 1
            continue

        explicit_markdown_heading = bool(re.match(r"^#{1,6}\s+", line))
        inferred_heading = (
            not explicit_markdown_heading
            and len(line) <= 120
            and blank_run >= 3
        )
        if inferred_heading:
            flush_block()
            rendered_blocks.append(f"## {line}")
        elif explicit_markdown_heading:
            flush_block()
            rendered_blocks.append(line)
        else:
            current_block.append(line)
        blank_run = 0

    flush_block()
    return "\n\n".join(block for block in rendered_blocks if block).strip()


def _tag_name(node: object) -> str:
    tag = getattr(node, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _escape_markdown_text(value: str | None) -> str:
    text = html.unescape(value or "").replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"([\\`*_\[\]])", r"\\\1", text)


def _clean_inline(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _safe_markdown_url(value: str | None, *, image: bool = False) -> str:
    url = html.unescape(value or "").strip()
    if not url or re.match(r"(?i)^(?:javascript|vbscript):", url):
        return ""
    if image and re.match(r"(?i)^data:", url):
        return ""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _render_inline_children(
    node: object,
    *,
    skip_lists: bool = False,
    image_url_map: dict[str, str] | None = None,
) -> str:
    parts = [_escape_markdown_text(getattr(node, "text", None))]
    for child in node:
        tag = _tag_name(child)
        if skip_lists and tag in {"ul", "ol"}:
            pass
        else:
            parts.append(_render_inline_node(child, image_url_map=image_url_map))
        parts.append(_escape_markdown_text(getattr(child, "tail", None)))
    return _clean_inline("".join(parts))


def _render_inline_node(
    node: object, image_url_map: dict[str, str] | None = None
) -> str:
    tag = _tag_name(node)
    inner = _render_inline_children(node, image_url_map=image_url_map)

    if tag == "br":
        return "\n"
    if tag == "img":
        source_url = getattr(node, "get")("src") or getattr(node, "get")("data-src")
        src = _safe_markdown_url(
            localize_asset_url(source_url, image_url_map), image=True
        )
        if not src:
            return ""
        alt = _escape_markdown_text(getattr(node, "get")("alt") or "記事内画像")
        return f"![{alt}]({src})"
    if tag == "a":
        href = _safe_markdown_url(getattr(node, "get")("href"))
        label = inner or _escape_markdown_text(href)
        return f"[{label}]({href})" if href else label
    if tag in {"strong", "b"}:
        return f"**{inner}**" if inner else ""
    if tag in {"em", "i"}:
        return f"*{inner}*" if inner else ""
    if tag == "del":
        return f"~~{inner}~~" if inner else ""
    if tag == "code":
        fence = "``" if "`" in inner else "`"
        return f"{fence}{inner}{fence}" if inner else ""
    if tag in {"iframe", "input", "select", "option", "textarea", "video", "audio", "form"}:
        # Preserve controls and embeds as raw HTML in the searchable copy.
        return (
            lxml_html.tostring(node, encoding="unicode", method="html")
            if lxml_html is not None
            else inner
        )
    return inner


def _render_list(
    node: object,
    depth: int = 0,
    image_url_map: dict[str, str] | None = None,
) -> str:
    ordered = _tag_name(node) == "ol"
    try:
        start = int(getattr(node, "get")("start") or 1)
    except (TypeError, ValueError):
        start = 1

    lines: list[str] = []
    item_index = 0
    for item in node:
        if _tag_name(item) != "li":
            continue
        marker = f"{start + item_index}. " if ordered else "- "
        item_index += 1
        inline = _render_inline_children(
            item, skip_lists=True, image_url_map=image_url_map
        )
        inline_lines = inline.splitlines() or [""]
        indent = "  " * depth
        lines.append(f"{indent}{marker}{inline_lines[0]}".rstrip())
        continuation = indent + " " * len(marker)
        lines.extend(f"{continuation}{line}".rstrip() for line in inline_lines[1:])
        for child in item:
            if _tag_name(child) in {"ul", "ol"}:
                nested = _render_list(child, depth + 1, image_url_map=image_url_map)
                if nested:
                    lines.append(nested)
    return "\n".join(lines)


def _render_blockquote(
    node: object, image_url_map: dict[str, str] | None = None
) -> str:
    body = _render_container(node, image_url_map=image_url_map).strip()
    if not body:
        return ""
    quoted = "\n".join("> " + line if line else ">" for line in body.splitlines())
    return f"{quoted}\n\n"


def _render_block_node(
    node: object, image_url_map: dict[str, str] | None = None
) -> str:
    tag = _tag_name(node)

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = min(int(tag[1]) + 1, 6)
        title = _render_inline_children(node, image_url_map=image_url_map)
        return f"{'#' * level} {title}\n\n" if title else ""
    if tag == "p":
        body = _render_inline_children(node, image_url_map=image_url_map)
        return f"{body}\n\n" if body else ""
    if tag in {"ul", "ol"}:
        body = _render_list(node, image_url_map=image_url_map)
        return f"{body}\n\n" if body else ""
    if tag == "blockquote":
        return _render_blockquote(node, image_url_map=image_url_map)
    if tag == "hr":
        return "---\n\n"
    if tag == "pre":
        code = "".join(getattr(node, "itertext")()).strip("\n")
        if not code:
            return ""
        fence = "````" if "```" in code else "```"
        return f"{fence}\n{code}\n{fence}\n\n"
    if tag == "table" and lxml_html is not None:
        table_html = lxml_html.tostring(node, encoding="unicode", method="html").strip()
        return f"{localize_html_image_urls(table_html, image_url_map)}\n\n"
    if tag in {"div", "section", "article", "figure", "figcaption", "main"}:
        return _render_container(node, image_url_map=image_url_map)
    if tag == "img":
        body = _render_inline_node(node, image_url_map=image_url_map)
        return f"{body}\n\n" if body else ""

    body = _render_inline_node(node, image_url_map=image_url_map)
    return f"{body}\n\n" if body else ""


def _render_container(
    node: object, image_url_map: dict[str, str] | None = None
) -> str:
    parts: list[str] = []
    leading = _escape_markdown_text(getattr(node, "text", None)).strip()
    if leading:
        parts.append(f"{leading}\n\n")
    for child in node:
        parts.append(_render_block_node(child, image_url_map=image_url_map))
        tail = _escape_markdown_text(getattr(child, "tail", None)).strip()
        if tail:
            parts.append(f"{tail}\n\n")
    return "".join(parts)


def source_body_to_markdown(
    body: str, image_url_map: dict[str, str] | None = None
) -> str:
    """Convert source rich HTML to readable Markdown while preserving structure."""
    if not body:
        return ""
    if lxml_html is None or not re.search(r"</?[a-z][^>]*>", body, flags=re.I):
        return _fallback_source_body_to_markdown(body, image_url_map=image_url_map)
    try:
        root = lxml_html.fragment_fromstring(body, create_parent="div")
        rendered = _render_container(root, image_url_map=image_url_map)
    except (ValueError, TypeError):
        return _fallback_source_body_to_markdown(body, image_url_map=image_url_map)

    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_authenticated_body_records(path: Path) -> dict[str, dict]:
    """Load the latest non-empty verified full-body record for each article."""
    if not path.exists():
        return {}

    records: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"warning: invalid authenticated body cache line "
                    f"{line_number}: {exc}",
                    file=sys.stderr,
                )
                continue
            object_id = str(record.get("object_id") or "")
            page_id = str(record.get("page_id") or "")
            body_html = record.get("html")
            if (
                record.get("status") != "ok"
                or not object_id
                or not isinstance(body_html, str)
                or not body_html.strip()
            ):
                continue
            if page_id and page_id != object_id:
                print(
                    f"warning: authenticated body ID mismatch: "
                    f"object_id={object_id} page_id={page_id}",
                    file=sys.stderr,
                )
                continue
            record = dict(record)
            record["object_id"] = object_id
            record["html_sha256"] = record.get("html_sha256") or sha256_text(body_html)
            records[object_id] = record
    return records


def load_authenticated_bodies(path: Path) -> dict[str, str]:
    """Backward-compatible view of the authenticated body cache."""
    return {
        object_id: record["html"]
        for object_id, record in load_authenticated_body_records(path).items()
    }


def load_jsonl_latest(path: Path, key_fields: tuple[str, ...]) -> dict[tuple[str, ...], dict]:
    """Load the latest valid JSON object for each composite key."""
    if not path.exists():
        return {}
    latest: dict[tuple[str, ...], dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"warning: invalid JSONL line {line_number} in {path}: {exc}", file=sys.stderr)
                continue
            key = tuple(str(record.get(field) or "") for field in key_fields)
            if all(key):
                latest[key] = record
    return latest


def load_asset_records(path: Path) -> dict[str, dict[str, dict]]:
    """Load the latest asset record by article ID and source URL."""
    latest = load_jsonl_latest(path, ("object_id", "source_url"))
    grouped: dict[str, dict[str, dict]] = {}
    for (object_id, source_url), record in latest.items():
        per_article = grouped.setdefault(object_id, {})
        for key in {canonical_asset_url(source_url), asset_alias_url(source_url)}:
            if not key:
                continue
            previous = per_article.get(key)
            if previous and previous.get("status") == "ok" and record.get("status") != "ok":
                continue
            per_article[key] = record
    return grouped


def load_collection_failures(path: Path) -> dict[str, dict]:
    """Load the latest collection failure by article ID."""
    latest = load_jsonl_latest(path, ("object_id",))
    return {key[0]: record for key, record in latest.items()}


def extract_body_image_urls(body: str, article_url: str, thumbnail_url: str | None = None) -> list[str]:
    """Return unique image URLs referenced by article-body image elements."""
    urls: list[str] = []
    seen: set[str] = set()
    thumbnail = canonical_asset_url(thumbnail_url)
    for match in re.finditer(r"<img\b([^>]*)>", body or "", flags=re.I | re.S):
        attrs = match.group(1)
        for attr in ("src", "data-src", "srcset", "data-srcset"):
            attr_match = re.search(
                rf"\b{attr}\s*=\s*([\"'])(.*?)\1", attrs, flags=re.I | re.S
            )
            if not attr_match:
                continue
            raw_values = attr_match.group(2).split(",") if "srcset" in attr else [attr_match.group(2)]
            for raw_value in raw_values:
                candidate = raw_value.strip().split(" ", 1)[0]
                resolved = canonical_asset_url(urllib.parse.urljoin(article_url, candidate))
                if (
                    resolved
                    and re.match(r"^https?://", resolved, flags=re.I)
                    and resolved != thumbnail
                    and resolved not in seen
                ):
                    seen.add(resolved)
                    urls.append(resolved)
    return urls


def article_asset_map(
    article: Article,
    body_html: str,
    records: dict[str, dict[str, dict]],
    local_root: Path | None = None,
) -> tuple[dict[str, str], str]:
    """Return local image replacements and the asset completeness status."""
    article_url = f"https://daigovideolab.jp/blog/{article.object_id}"
    urls = extract_body_image_urls(body_html, article_url, article.thumbnail_url)
    if not urls:
        return {}, ASSET_STATUS_NONE

    replacements: dict[str, str] = {}
    for source_url in urls:
        article_records = records.get(article.object_id, {})
        record = article_records.get(source_url, {})
        if record.get("status") != "ok":
            record = article_records.get(asset_alias_url(source_url), {})
        local_file = str(record.get("local_file") or "")
        local_path = Path(local_file)
        if local_root is not None and not local_path.is_absolute():
            local_path = local_root / local_path
        if (
            record.get("status") == "ok"
            and local_file
            and (local_root is None or local_path.is_file())
        ):
            replacements[source_url] = local_file
    status = ASSET_STATUS_COMPLETE if len(replacements) == len(urls) else ASSET_STATUS_PARTIAL
    return replacements, status


def build_markdown(
    article: Article,
    authenticated_html: str | None = None,
    *,
    image_url_map: dict[str, str] | None = None,
    collection_status: str | None = None,
    asset_status: str | None = None,
    raw_html_file: str | None = None,
) -> str:
    date_full, _ = date_parts(article.regist_ms)
    url = f"https://daigovideolab.jp/blog/{article.object_id}"
    source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB
    body = authenticated_html if authenticated_html is not None else article.body
    source_body = source_body_to_markdown(body, image_url_map=image_url_map)
    content_source = (
        CONTENT_SOURCE_AUTHENTICATED if authenticated_html is not None else CONTENT_SOURCE_INDEX
    )
    content_completeness = "full" if authenticated_html is not None else "partial"
    collection_status = collection_status or (
        COLLECTION_STATUS_FULL
        if authenticated_html is not None
        else COLLECTION_STATUS_PARTIAL
    )
    asset_status = asset_status or ASSET_STATUS_NONE

    lines = [
        "---",
        f"source: {yaml_quote(DLAB)}",
        f"url: {yaml_quote(url)}",
        f"date: {yaml_quote(date_full)}",
        f"channel: {yaml_quote(PALEO)}",
        f"type: {yaml_quote(ARTICLE)}",
        f"object_id: {yaml_quote(article.object_id)}",
        f"origin: {yaml_quote(article.origin) if article.origin else 'null'}",
        f"source_type: {yaml_quote(source_type)}",
        (
            f"thumbnail_url: {yaml_quote(article.thumbnail_url)}"
            if article.thumbnail_url
            else "thumbnail_url: null"
        ),
        f"content_source: {yaml_quote(content_source)}",
        f"content_completeness: {yaml_quote(content_completeness)}",
        f"collection_status: {yaml_quote(collection_status)}",
        f"asset_status: {yaml_quote(asset_status)}",
        f"raw_html_file: {yaml_quote(raw_html_file) if raw_html_file else 'null'}",
        "---",
        "",
        f"# {article.title}",
        "",
    ]
    lines.extend([source_body or "*本文未取得*", ""])
    return "\n".join(lines)


def _write_text_if_changed(path: Path, value: str, overwrite: bool) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") == value:
            return False
        if not overwrite:
            raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")
    return True


def _write_raw_html(path: Path, value: str, overwrite: bool) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") == value:
            return False
        if not overwrite:
            raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
    return True


def _load_manifest_paths(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("object_id") or ""): str(row.get("file") or "")
            for row in csv.DictReader(handle)
            if row.get("object_id")
        }


def reset_outputs(
    out_dir: Path,
    state_dir: Path,
    incomplete_dir: Path | None = None,
) -> None:
    """Archive generated outputs before a rebuild instead of deleting them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    incomplete_dir = incomplete_dir or out_dir.parent / "articles_incomplete"
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%dT%H%M%S%f")
    legacy_dir = state_dir / "legacy" / stamp

    for source_dir, label in ((out_dir, "articles"), (incomplete_dir, "incomplete")):
        for path in source_dir.glob("*.md"):
            destination = legacy_dir / label / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
    raw_dir = out_dir / "raw_html"
    if raw_dir.exists():
        destination = legacy_dir / "raw_html"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(raw_dir), str(destination))
    for name in [
        "manifest.csv",
        "inventory.csv",
        "skipped.csv",
        "collection_failures.csv",
        "progress.json",
    ]:
        path = state_dir / name
        if path.exists():
            destination = legacy_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))


def write_outputs(
    records: list[Article],
    out_dir: Path,
    state_dir: Path,
    overwrite: bool,
    origin_null_only: bool,
    authenticated_records: dict[str, dict] | None = None,
    asset_records: dict[str, dict[str, dict]] | None = None,
    collection_failures: dict[str, dict] | None = None,
    incomplete_dir: Path | None = None,
    raw_html_dir: Path | None = None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    incomplete_dir = incomplete_dir or out_dir.parent / "articles_incomplete"
    raw_html_dir = raw_html_dir or out_dir / "raw_html"
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    raw_html_dir.mkdir(parents=True, exist_ok=True)
    authenticated_records = authenticated_records or {}
    authenticated_records = {
        object_id: (
            {"status": "ok", "html": value}
            if isinstance(value, str)
            else value
        )
        for object_id, value in authenticated_records.items()
    }
    asset_records = asset_records or {}
    collection_failures = collection_failures or {}

    target_records = [article for article in records if not origin_null_only or not article.origin]
    excluded_records = [article for article in records if origin_null_only and article.origin]
    target_ids = [article.object_id for article in target_records]
    if len(target_ids) != len(set(target_ids)):
        raise RuntimeError("duplicate object_id in target inventory")

    existing_manifest_paths = _load_manifest_paths(state_dir / "manifest.csv")
    existing_names = {p.name for p in out_dir.glob("*.md")}
    incomplete_names = {p.name for p in incomplete_dir.glob("*.md")}
    created = incomplete_created = skipped = errors = 0
    full = asset_failed = partial = pending = failed = 0
    manifest_rows: list[dict] = []
    skip_rows: list[dict] = []

    def new_filename(
        article: Article, names: set[str], destination_dir: Path
    ) -> str:
        previous = Path(existing_manifest_paths.get(article.object_id, "")).name
        previous_parent = Path(existing_manifest_paths.get(article.object_id, "")).parent.name
        if previous and previous.endswith(".md") and previous_parent == destination_dir.name:
            names.add(previous)
            return previous
        _, date_compact = date_parts(article.regist_ms)
        return safe_filename(date_compact, article.title, article.object_id, names)

    for idx, article in enumerate(target_records, 1):
        try:
            source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB
            authenticated_record = authenticated_records.get(article.object_id)
            authenticated_html = (
                str(authenticated_record.get("html"))
                if authenticated_record and isinstance(authenticated_record.get("html"), str)
                and authenticated_record.get("html").strip()
                else None
            )
            failure_record = collection_failures.get(article.object_id, {})

            if authenticated_html is not None:
                image_map, asset_status = article_asset_map(
                    article, authenticated_html, asset_records, local_root=out_dir
                )
                image_urls = extract_body_image_urls(
                    authenticated_html,
                    f"https://daigovideolab.jp/blog/{article.object_id}",
                    article.thumbnail_url,
                )
                collection_status = (
                    COLLECTION_STATUS_FULL
                    if asset_status != ASSET_STATUS_PARTIAL
                    else COLLECTION_STATUS_ASSET_FAILED
                )
                content_source = CONTENT_SOURCE_AUTHENTICATED
                content_completeness = "full"
                raw_html_path = raw_html_dir / f"{article.object_id}.html"
                raw_html_file = str(raw_html_path)
                raw_html_created = _write_raw_html(
                    raw_html_path, authenticated_html, overwrite
                )
                if collection_status == COLLECTION_STATUS_FULL:
                    full += 1
                    destination_dir = out_dir
                    names = existing_names
                else:
                    asset_failed += 1
                    destination_dir = incomplete_dir
                    names = incomplete_names
                asset_failure = (
                    "missing local asset(s): "
                    + ", ".join(url for url in image_urls if url not in image_map)
                    if asset_status == ASSET_STATUS_PARTIAL
                    else ""
                )
            else:
                image_map = {}
                asset_status = ASSET_STATUS_NONE
                raw_html_file = ""
                raw_html_created = False
                content_source = CONTENT_SOURCE_INDEX
                content_completeness = "partial"
                if failure_record.get("status") == "failed":
                    collection_status = COLLECTION_STATUS_FAILED
                    failed += 1
                    destination_dir = None
                    names = existing_names
                elif article.body:
                    collection_status = COLLECTION_STATUS_PARTIAL
                    partial += 1
                    destination_dir = incomplete_dir
                    names = incomplete_names
                else:
                    collection_status = COLLECTION_STATUS_PENDING
                    pending += 1
                    destination_dir = None
                    names = existing_names

            body = authenticated_html if authenticated_html is not None else article.body
            cleaned = clean_body(body)
            headings = extract_headings(cleaned)
            date_full, _ = date_parts(article.regist_ms)
            path = None
            if destination_dir is not None:
                filename = new_filename(article, names, destination_dir)
                path = destination_dir / filename
                markdown = build_markdown(
                    article,
                    authenticated_html,
                    image_url_map=image_map,
                    collection_status=collection_status,
                    asset_status=asset_status,
                    raw_html_file=raw_html_file,
                )
                if _write_text_if_changed(path, markdown, overwrite):
                    if destination_dir == out_dir:
                        created += 1
                    else:
                        incomplete_created += 1
            failure_reason = str(
                failure_record.get("reason")
                or (asset_failure if authenticated_html is not None else "")
            )
            manifest_rows.append(
                {
                    "object_id": article.object_id,
                    "title": article.title,
                    "date": date_full,
                    "url": f"https://daigovideolab.jp/blog/{article.object_id}",
                    "file": str(path) if path else "",
                    "html_file": raw_html_file,
                    "headings": len(headings),
                    "bytes": len(markdown.encode("utf-8")) if path else 0,
                    "raw_html_sha256": sha256_text(authenticated_html) if authenticated_html is not None else "",
                    "origin": article.origin or "",
                    "source_type": source_type,
                    "thumbnail_url": article.thumbnail_url or "",
                    "content_source": content_source,
                    "content_completeness": content_completeness,
                    "collection_status": collection_status,
                    "asset_status": asset_status,
                    "asset_count": len(image_urls) if authenticated_html is not None else 0,
                    "body_chars": len(cleaned),
                    "failure_reason": failure_reason,
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            failed += 1
            skip_rows.append(
                {
                    "object_id": article.object_id,
                    "title": article.title,
                    "origin": article.origin or "",
                    "source_type": SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB,
                    "reason": f"error: {exc}",
                }
            )
            manifest_rows.append(
                {
                    "object_id": article.object_id,
                    "title": article.title,
                    "date": date_parts(article.regist_ms)[0],
                    "url": f"https://daigovideolab.jp/blog/{article.object_id}",
                    "file": "",
                    "html_file": "",
                    "headings": 0,
                    "bytes": 0,
                    "raw_html_sha256": "",
                    "origin": article.origin or "",
                    "source_type": SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB,
                    "thumbnail_url": article.thumbnail_url or "",
                    "content_source": CONTENT_SOURCE_INDEX,
                    "content_completeness": "partial",
                    "collection_status": COLLECTION_STATUS_FAILED,
                    "asset_status": ASSET_STATUS_NONE,
                    "asset_count": 0,
                    "body_chars": 0,
                    "failure_reason": f"error: {exc}",
                }
            )
        if idx % 500 == 0:
            print(
                f"processed={idx} created={created} incomplete={incomplete_created} "
                f"full={full} partial={partial} pending={pending} failed={failed} errors={errors}",
                flush=True,
            )

    for article in excluded_records:
        skipped += 1
        skip_rows.append(
            {
                "object_id": article.object_id,
                "title": article.title,
                "origin": article.origin or "",
                "source_type": SOURCE_TYPE_BLOG,
            "reason": "non_null_origin",
        }
    )

    def resolve_output_value(value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else out_dir.parent / path).resolve()

    desired_files = {
        resolve_output_value(row["file"])
        for row in manifest_rows
        if row.get("file")
    }
    stale_root = state_dir / "legacy" / "stale" / datetime.now(JST).strftime("%Y%m%dT%H%M%S%f")
    for previous_value in existing_manifest_paths.values():
        if not previous_value:
            continue
        previous_path = Path(previous_value)
        if not previous_path.is_absolute():
            previous_path = out_dir.parent / previous_path
        if (
            previous_path.suffix == ".md"
            and previous_path.exists()
            and previous_path.resolve() not in desired_files
            and previous_path.resolve().parent in {
                out_dir.resolve(),
                incomplete_dir.resolve(),
            }
        ):
            destination = stale_root / previous_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(previous_path), str(destination))

    desired_html = {
        resolve_output_value(row["html_file"])
        for row in manifest_rows
        if row.get("html_file")
    }
    for previous_path in raw_html_dir.glob("*.html"):
        if previous_path.resolve() not in desired_html:
            destination = stale_root / "raw_html" / previous_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(previous_path), str(destination))

    manifest_path = state_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "object_id",
            "title",
            "date",
            "url",
            "file",
            "html_file",
            "headings",
            "bytes",
            "raw_html_sha256",
            "origin",
            "source_type",
            "thumbnail_url",
            "content_source",
            "content_completeness",
            "collection_status",
            "asset_status",
            "asset_count",
            "body_chars",
            "failure_reason",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest_rows)

    skipped_path = state_dir / "skipped.csv"
    if not excluded_records and skipped_path.exists():
        # A local D-Lab-only manifest cannot reconstruct the out-of-scope blog
        # rows. Preserve their prior exclusion ledger on local-only reruns.
        with skipped_path.open("r", encoding="utf-8-sig", newline="") as handle:
            previous_skips = list(csv.DictReader(handle))
        preserved_skips = [
            row
            for row in previous_skips
            if row.get("reason") == "non_null_origin"
            and row.get("source_type") == SOURCE_TYPE_BLOG
        ]
        if preserved_skips:
            skip_rows = preserved_skips + skip_rows
            skipped += len(preserved_skips)
    with skipped_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["object_id", "title", "origin", "source_type", "reason"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(skip_rows)

    full = sum(row.get("collection_status") == COLLECTION_STATUS_FULL for row in manifest_rows)
    asset_failed = sum(
        row.get("collection_status") == COLLECTION_STATUS_ASSET_FAILED
        for row in manifest_rows
    )
    partial = sum(row.get("collection_status") == COLLECTION_STATUS_PARTIAL for row in manifest_rows)
    pending = sum(row.get("collection_status") == COLLECTION_STATUS_PENDING for row in manifest_rows)
    failed = sum(row.get("collection_status") == COLLECTION_STATUS_FAILED for row in manifest_rows)

    failures_path = state_dir / "collection_failures.csv"
    failure_rows = []
    for row in manifest_rows:
        if row.get("collection_status") in {
            COLLECTION_STATUS_FAILED,
            COLLECTION_STATUS_PENDING,
            COLLECTION_STATUS_ASSET_FAILED,
        }:
            failure_rows.append(
                {
                    "object_id": row["object_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "status": row["collection_status"],
                    "reason": row.get("failure_reason") or "not yet captured",
                }
            )
    with failures_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["object_id", "title", "url", "status", "reason"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(failure_rows)

    progress = {
        "records": len(target_records),
        "fetched_records": len(records),
        "created": created,
        "incomplete_created": incomplete_created,
        "skipped": skipped,
        "errors": errors,
        "full": full,
        "asset_failed": asset_failed,
        "partial": partial,
        "pending": pending,
        "failed": failed,
        "authenticated_bodies": len(authenticated_records),
        "manifest": str(manifest_path),
        "skipped_file": str(skipped_path),
        "failures_file": str(failures_path),
        "updated_at": datetime.now(JST).isoformat(),
    }
    (state_dir / "progress.json").write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return progress


def validate_inventory(records: list[Article]) -> None:
    ids = [article.object_id for article in records]
    if not ids or any(not object_id for object_id in ids):
        raise RuntimeError("Algolia inventory contains an empty object_id")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Algolia inventory contains duplicate object_id values")


def write_inventory(
    records: list[Article], state_dir: Path, origin_null_only: bool, refresh: bool = False
) -> tuple[Path, str, int]:
    """Write the frozen metadata queue consumed by the authenticated collector."""
    target_records = [article for article in records if not origin_null_only or not article.origin]
    path = state_dir / "inventory.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["object_id", "title", "date", "url", "origin", "thumbnail_url", "source_type"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for article in target_records:
        writer.writerow(
            {
                "object_id": article.object_id,
                "title": article.title,
                "date": date_parts(article.regist_ms)[0],
                "url": f"https://daigovideolab.jp/blog/{article.object_id}",
                "origin": article.origin or "",
                "thumbnail_url": article.thumbnail_url or "",
                "source_type": SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB,
            }
        )
    content = buffer.getvalue()
    if path.exists() and not refresh:
        existing = path.read_text(encoding="utf-8-sig")
        if existing != content:
            raise RuntimeError(
                f"frozen inventory differs: {path}; use --refresh-inventory only after review"
            )
    else:
        path.write_text("\ufeff" + content, encoding="utf-8", newline="")
    return path, sha256_text(content), len(target_records)


def timestamp_from_manifest_date(value: str) -> int:
    value = (value or "").strip()
    for fmt, length in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return int(datetime.strptime(value[:length], fmt).replace(tzinfo=JST).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def body_from_existing_markdown(path: Path | None, thumbnail_url: str | None) -> str:
    if path is None or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) == 3 else text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            text = "\n".join(lines[index + 1 :])
            break
    thumbnail = canonical_asset_url(thumbnail_url)
    if thumbnail:
        text = re.sub(
            rf"!\[[^\]]*\]\({re.escape(thumbnail)}\)\s*",
            "",
            text,
        )
    text = text.strip()
    if text in {"*取得できる本文なし*", "*本文未取得*"}:
        return ""
    return text


def load_records_from_manifest(path: Path) -> list[Article]:
    """Reconstruct an Article inventory from an existing local manifest."""
    root = path.parent.parent
    records: list[Article] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            object_id = str(row.get("object_id") or "").strip()
            if not object_id:
                continue
            file_value = str(row.get("file") or "")
            file_path = None
            if file_value:
                file_path = Path(file_value.replace("\\", "/"))
                if not file_path.is_absolute():
                    file_path = root / file_path
            records.append(
                Article(
                    object_id=object_id,
                    title=str(row.get("title") or "").strip(),
                    body=body_from_existing_markdown(
                        file_path, row.get("thumbnail_url") or None
                    ),
                    regist_ms=timestamp_from_manifest_date(str(row.get("date") or "")),
                    channel_owner="paleo",
                    origin=str(row.get("origin") or "") or None,
                    thumbnail_url=str(row.get("thumbnail_url") or "") or None,
                )
            )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="articles")
    parser.add_argument("--incomplete-dir", default="articles_incomplete")
    parser.add_argument("--raw-html-dir", default="articles/raw_html")
    parser.add_argument("--state-dir", default="export_state")
    parser.add_argument(
        "--local-manifest",
        default=None,
        help="Rebuild from an existing local manifest without contacting Algolia.",
    )
    parser.add_argument("--latest", type=int, default=None)
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--reset-out-dir", action="store_true")
    parser.add_argument(
        "--origin-null-only",
        action="store_true",
        help="Limit output to D-Lab internal articles (the default scope).",
    )
    parser.add_argument(
        "--include-blog-origin",
        action="store_true",
        help="Opt in to Paleo blog-origin records; excluded by default.",
    )
    parser.add_argument(
        "--refresh-inventory",
        action="store_true",
        help="Replace an existing frozen inventory after reviewing the ID-set change.",
    )
    parser.add_argument(
        "--authenticated-bodies",
        default=None,
        help="JSONL cache of verified authenticated article-body HTML",
    )
    parser.add_argument(
        "--asset-records",
        default=None,
        help="JSONL cache of downloaded article-body image assets",
    )
    parser.add_argument(
        "--collection-failures",
        default=None,
        help="JSONL cache of authenticated collection failures",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    incomplete_dir = Path(args.incomplete_dir)
    raw_html_dir = Path(args.raw_html_dir)
    state_dir = Path(args.state_dir)
    origin_null_only = not args.include_blog_origin or args.origin_null_only
    authenticated_body_path = (
        Path(args.authenticated_bodies)
        if args.authenticated_bodies
        else state_dir / "authenticated_bodies.jsonl"
    )
    asset_records_path = (
        Path(args.asset_records) if args.asset_records else state_dir / "assets.jsonl"
    )
    collection_failures_path = (
        Path(args.collection_failures)
        if args.collection_failures
        else state_dir / "collection_failures.jsonl"
    )
    local_manifest_path = Path(args.local_manifest) if args.local_manifest else None
    if local_manifest_path:
        records = load_records_from_manifest(local_manifest_path)
    else:
        records = fetch_recent(args.latest) if args.latest else fetch_all(args.max_records)
    if args.reset_out_dir:
        reset_outputs(out_dir, state_dir, incomplete_dir)
    validate_inventory(records)
    if not local_manifest_path and args.latest is None and args.max_records is None:
        expected_records = fetch_count()
        if len(records) != expected_records:
            raise RuntimeError(
                f"Algolia inventory count mismatch: expected={expected_records} got={len(records)}"
            )
    _inventory_path, inventory_sha256, inventory_count = write_inventory(
        records, state_dir, origin_null_only, refresh=args.refresh_inventory
    )
    authenticated_records = load_authenticated_body_records(authenticated_body_path)
    asset_records = load_asset_records(asset_records_path)
    collection_failures = load_collection_failures(collection_failures_path)
    print(f"fetched={len(records)}")
    print(f"inventory={inventory_count} sha256={inventory_sha256}")
    print(f"authenticated_bodies={len(authenticated_records)}")
    progress = write_outputs(
        records,
        out_dir,
        state_dir,
        args.overwrite,
        origin_null_only,
        authenticated_records,
        asset_records,
        collection_failures,
        incomplete_dir,
        raw_html_dir,
    )
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
