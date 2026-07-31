#!/usr/bin/env python3
"""Export D-Lab Paleo articles into structure-preserving Markdown.

Verified authenticated HTML is preferred when available. The Algolia search
index remains the fallback, and the exporter never invents missing content.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
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
        if count > 950 and hi - lo > 24 * 60 * 60 * 1000:
            mid = (lo + hi) // 2
            ranges.append((lo, mid))
            ranges.append((mid, hi))
            continue
        pages = (count + 99) // 100
        for page in range(pages):
            data = query_page(page, 100, f"blog_regist_time >= {lo} AND blog_regist_time < {hi}")
            for hit in data.get("hits", []):
                article = article_from_hit(hit)
                if article.object_id in seen:
                    continue
                seen.add(article.object_id)
                records.append(article)
                if max_records and len(records) >= max_records:
                    return sorted(records, key=lambda x: x.regist_ms, reverse=True)
            time.sleep(0.02)
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


def _fallback_source_body_to_markdown(body: str) -> str:
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
        return f"\n\n![{alt}]({src_match.group(1).strip()})\n\n"

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
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|ul|ol|blockquote)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)

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


def _render_inline_children(node: object, *, skip_lists: bool = False) -> str:
    parts = [_escape_markdown_text(getattr(node, "text", None))]
    for child in node:
        tag = _tag_name(child)
        if skip_lists and tag in {"ul", "ol"}:
            pass
        else:
            parts.append(_render_inline_node(child))
        parts.append(_escape_markdown_text(getattr(child, "tail", None)))
    return _clean_inline("".join(parts))


def _render_inline_node(node: object) -> str:
    tag = _tag_name(node)
    inner = _render_inline_children(node)

    if tag == "br":
        return "\n"
    if tag == "img":
        src = _safe_markdown_url(getattr(node, "get")("src"), image=True)
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
    return inner


def _render_list(node: object, depth: int = 0) -> str:
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
        inline = _render_inline_children(item, skip_lists=True)
        inline_lines = inline.splitlines() or [""]
        indent = "  " * depth
        lines.append(f"{indent}{marker}{inline_lines[0]}".rstrip())
        continuation = indent + " " * len(marker)
        lines.extend(f"{continuation}{line}".rstrip() for line in inline_lines[1:])
        for child in item:
            if _tag_name(child) in {"ul", "ol"}:
                nested = _render_list(child, depth + 1)
                if nested:
                    lines.append(nested)
    return "\n".join(lines)


def _render_blockquote(node: object) -> str:
    body = _render_container(node).strip()
    if not body:
        return ""
    quoted = "\n".join("> " + line if line else ">" for line in body.splitlines())
    return f"{quoted}\n\n"


def _render_block_node(node: object) -> str:
    tag = _tag_name(node)

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = min(int(tag[1]) + 1, 6)
        title = _render_inline_children(node)
        return f"{'#' * level} {title}\n\n" if title else ""
    if tag == "p":
        body = _render_inline_children(node)
        return f"{body}\n\n" if body else ""
    if tag in {"ul", "ol"}:
        body = _render_list(node)
        return f"{body}\n\n" if body else ""
    if tag == "blockquote":
        return _render_blockquote(node)
    if tag == "hr":
        return "---\n\n"
    if tag == "pre":
        code = "".join(getattr(node, "itertext")()).strip("\n")
        if not code:
            return ""
        fence = "````" if "```" in code else "```"
        return f"{fence}\n{code}\n{fence}\n\n"
    if tag == "table" and lxml_html is not None:
        return f"{lxml_html.tostring(node, encoding='unicode', method='html').strip()}\n\n"
    if tag in {"div", "section", "article", "figure", "figcaption", "main"}:
        return _render_container(node)
    if tag == "img":
        body = _render_inline_node(node)
        return f"{body}\n\n" if body else ""

    body = _render_inline_node(node)
    return f"{body}\n\n" if body else ""


def _render_container(node: object) -> str:
    parts: list[str] = []
    leading = _escape_markdown_text(getattr(node, "text", None)).strip()
    if leading:
        parts.append(f"{leading}\n\n")
    for child in node:
        parts.append(_render_block_node(child))
        tail = _escape_markdown_text(getattr(child, "tail", None)).strip()
        if tail:
            parts.append(f"{tail}\n\n")
    return "".join(parts)


def source_body_to_markdown(body: str) -> str:
    """Convert source rich HTML to readable Markdown while preserving structure."""
    if not body:
        return ""
    if lxml_html is None or not re.search(r"</?[a-z][^>]*>", body, flags=re.I):
        return _fallback_source_body_to_markdown(body)
    try:
        root = lxml_html.fragment_fromstring(body, create_parent="div")
        rendered = _render_container(root)
    except (ValueError, TypeError):
        return _fallback_source_body_to_markdown(body)

    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def load_authenticated_bodies(path: Path) -> dict[str, str]:
    """Load the latest verified full-body HTML record for each article."""
    if not path.exists():
        return {}

    bodies: dict[str, str] = {}
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
            if record.get("status") != "ok" or not object_id or not isinstance(body_html, str):
                continue
            if page_id and page_id != object_id:
                print(
                    f"warning: authenticated body ID mismatch: "
                    f"object_id={object_id} page_id={page_id}",
                    file=sys.stderr,
                )
                continue
            bodies[object_id] = body_html
    return bodies


def build_markdown(article: Article, authenticated_html: str | None = None) -> str:
    date_full, _ = date_parts(article.regist_ms)
    url = f"https://daigovideolab.jp/blog/{article.object_id}"
    source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB
    source_body = source_body_to_markdown(authenticated_html or article.body)
    content_source = (
        CONTENT_SOURCE_AUTHENTICATED if authenticated_html is not None else CONTENT_SOURCE_INDEX
    )
    content_completeness = "full" if authenticated_html is not None else "unknown"

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
        "---",
        "",
        f"# {article.title}",
        "",
    ]
    if article.thumbnail_url and (
        authenticated_html is None
        or article.thumbnail_url not in html.unescape(authenticated_html)
    ):
        lines.extend([f"![記事サムネイル]({article.thumbnail_url})", ""])
    lines.extend([source_body or "*取得できる本文なし*", ""])
    return "\n".join(lines)


def reset_outputs(out_dir: Path, state_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.md"):
        path.unlink()
    for name in ["manifest.csv", "skipped.csv", "progress.json"]:
        path = state_dir / name
        if path.exists():
            path.unlink()


def write_outputs(
    records: list[Article],
    out_dir: Path,
    state_dir: Path,
    overwrite: bool,
    origin_null_only: bool,
    authenticated_bodies: dict[str, str],
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    existing_names = {p.name for p in out_dir.glob("*.md")}
    created = skipped = errors = 0
    manifest_rows: list[dict] = []
    skip_rows: list[dict] = []

    for idx, article in enumerate(records, 1):
        try:
            source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB
            authenticated_html = authenticated_bodies.get(article.object_id)
            if origin_null_only and article.origin:
                skipped += 1
                skip_rows.append(
                    {
                        "object_id": article.object_id,
                        "title": article.title,
                        "origin": article.origin,
                        "source_type": source_type,
                        "reason": "non_null_origin",
                    }
                )
                continue

            cleaned = clean_body(authenticated_html or article.body)
            headings = extract_headings(cleaned)
            if (not origin_null_only) and (not headings or (len(headings) == 1 and not strong_single_heading(headings[0]))):
                skipped += 1
                skip_rows.append(
                    {
                        "object_id": article.object_id,
                        "title": article.title,
                        "origin": article.origin or "",
                        "source_type": source_type,
                        "reason": "no_heading",
                    }
                )
                continue

            date_full, date_compact = date_parts(article.regist_ms)
            filename = safe_filename(date_compact, article.title, article.object_id, existing_names)
            path = out_dir / filename
            if path.exists() and not overwrite:
                skipped += 1
                skip_rows.append(
                    {
                        "object_id": article.object_id,
                        "title": article.title,
                        "origin": article.origin or "",
                        "source_type": source_type,
                        "reason": "exists",
                    }
                )
                continue

            markdown = build_markdown(article, authenticated_html)
            path.write_text(markdown, encoding="utf-8", newline="\n")
            created += 1
            manifest_rows.append(
                {
                    "object_id": article.object_id,
                    "title": article.title,
                    "date": date_full,
                    "url": f"https://daigovideolab.jp/blog/{article.object_id}",
                    "file": str(path),
                    "headings": len(headings),
                    "bytes": len(markdown.encode("utf-8")),
                    "origin": article.origin or "",
                    "source_type": source_type,
                    "thumbnail_url": article.thumbnail_url or "",
                    "content_source": (
                        CONTENT_SOURCE_AUTHENTICATED
                        if authenticated_html is not None
                        else CONTENT_SOURCE_INDEX
                    ),
                    "content_completeness": (
                        "full" if authenticated_html is not None else "unknown"
                    ),
                    "body_chars": len(cleaned),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            skip_rows.append(
                {
                    "object_id": article.object_id,
                    "title": article.title,
                    "origin": article.origin or "",
                    "source_type": SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB,
                    "reason": f"error: {exc}",
                }
            )
        if idx % 500 == 0:
            print(f"processed={idx} created={created} skipped={skipped} errors={errors}", flush=True)

    manifest_path = state_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "object_id",
            "title",
            "date",
            "url",
            "file",
            "headings",
            "bytes",
            "origin",
            "source_type",
            "thumbnail_url",
            "content_source",
            "content_completeness",
            "body_chars",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest_rows)

    skipped_path = state_dir / "skipped.csv"
    with skipped_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["object_id", "title", "origin", "source_type", "reason"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(skip_rows)

    progress = {
        "records": len(records),
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "authenticated_bodies": len(authenticated_bodies),
        "manifest": str(manifest_path),
        "skipped_file": str(skipped_path),
        "updated_at": datetime.now(JST).isoformat(),
    }
    (state_dir / "progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    return progress


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="articles")
    parser.add_argument("--state-dir", default="export_state")
    parser.add_argument("--latest", type=int, default=None)
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--reset-out-dir", action="store_true")
    parser.add_argument("--origin-null-only", action="store_true")
    parser.add_argument(
        "--authenticated-bodies",
        default=None,
        help="JSONL cache of verified authenticated article-body HTML",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    state_dir = Path(args.state_dir)
    authenticated_body_path = (
        Path(args.authenticated_bodies)
        if args.authenticated_bodies
        else state_dir / "authenticated_bodies.jsonl"
    )
    if args.reset_out_dir:
        reset_outputs(out_dir, state_dir)

    records = fetch_recent(args.latest) if args.latest else fetch_all(args.max_records)
    authenticated_bodies = load_authenticated_bodies(authenticated_body_path)
    print(f"fetched={len(records)}")
    print(f"authenticated_bodies={len(authenticated_bodies)}")
    progress = write_outputs(
        records,
        out_dir,
        state_dir,
        args.overwrite,
        args.origin_null_only,
        authenticated_bodies,
    )
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
