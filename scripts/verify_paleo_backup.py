#!/usr/bin/env python3
"""Verify the completeness and file integrity of a Paleo article backup."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path


FULL = "full"
ASSET_FAILED = "asset_failed"
PARTIAL = "partial"
PENDING = "pending"
FAILED = "failed"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_latest_assets(path: Path) -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid assets JSONL line {line_number}: {exc}") from exc
            key = (str(record.get("object_id") or ""), str(record.get("source_url") or ""))
            if all(key):
                latest[key] = record
    return latest


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


def extract_body_image_urls(body: str, article_url: str, thumbnail_url: str | None = None) -> list[str]:
    """Extract the same unique body-image URLs used by the exporter."""
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


def verify(root: Path, manifest_path: Path, assets_path: Path) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    rows = read_manifest(manifest_path)
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    referenced_markdown: set[Path] = set()
    referenced_html: set[Path] = set()
    assets = read_latest_assets(assets_path)
    asset_ok_aliases: dict[str, set[str]] = {}
    for (object_id, _source_url), record in assets.items():
        if record.get("status") == "ok":
            asset_ok_aliases.setdefault(object_id, set()).add(
                asset_alias_url(str(record.get("source_url") or ""))
            )

    for row in rows:
        object_id = str(row.get("object_id") or "")
        status = str(row.get("collection_status") or "")
        counts[status] = counts.get(status, 0) + 1
        if not object_id:
            errors.append("manifest contains an empty object_id")
            continue
        if object_id in seen_ids:
            errors.append(f"duplicate manifest object_id: {object_id}")
        seen_ids.add(object_id)
        if row.get("origin"):
            errors.append(f"out-of-scope origin in target manifest: {object_id}")
        if status not in {FULL, ASSET_FAILED, PARTIAL, PENDING, FAILED}:
            errors.append(f"unknown collection_status for {object_id}: {status}")

        markdown_value = str(row.get("file") or "")
        html_value = str(row.get("html_file") or "")
        markdown_path = root / markdown_value.replace("\\", "/") if markdown_value else None
        html_path = root / html_value.replace("\\", "/") if html_value else None

        if status == FULL:
            if row.get("content_completeness") != "full":
                errors.append(f"full row lacks full content_completeness: {object_id}")
            if not markdown_path or not markdown_path.exists():
                errors.append(f"full Markdown missing: {object_id}")
            else:
                referenced_markdown.add(markdown_path.resolve())
            if not html_path or not html_path.exists():
                errors.append(f"full HTML missing: {object_id}")
            else:
                referenced_html.add(html_path.resolve())
                expected_hash = str(row.get("raw_html_sha256") or "")
                if not expected_hash or sha256_file(html_path) != expected_hash:
                    errors.append(f"raw HTML hash mismatch: {object_id}")
            if row.get("asset_status") == "partial":
                errors.append(f"full row has partial assets: {object_id}")
            try:
                expected_assets = int(row.get("asset_count") or 0)
            except ValueError:
                errors.append(f"invalid asset_count: {object_id}")
                expected_assets = 0
            if row.get("asset_status") == "complete":
                body_urls: list[str] = []
                if html_path and html_path.exists():
                    body_urls = extract_body_image_urls(
                        html_path.read_text(encoding="utf-8"),
                        str(row.get("url") or f"https://daigovideolab.jp/blog/{object_id}"),
                        str(row.get("thumbnail_url") or ""),
                    )
                if len(body_urls) != expected_assets:
                    errors.append(
                        f"asset_count does not match raw HTML image count: {object_id} "
                        f"manifest={expected_assets} html={len(body_urls)}"
                    )
                missing_urls = [
                    source_url
                    for source_url in body_urls
                    if asset_alias_url(source_url)
                    not in asset_ok_aliases.get(object_id, set())
                ]
                if missing_urls:
                    errors.append(
                        f"complete assets are not backed by asset records: {object_id} "
                        f"missing={len(missing_urls)}"
                    )
            if row.get("asset_status") == "none" and expected_assets:
                errors.append(f"asset_count conflicts with none status: {object_id}")
        elif status == ASSET_FAILED:
            if row.get("content_completeness") != "full":
                errors.append(f"asset_failed row lacks full body marker: {object_id}")
            if not html_path or not html_path.exists():
                errors.append(f"asset_failed HTML missing: {object_id}")
            else:
                referenced_html.add(html_path.resolve())
            if markdown_path and markdown_path.exists():
                referenced_markdown.add(markdown_path.resolve())
        elif status == PARTIAL:
            if row.get("content_completeness") == "full":
                errors.append(f"partial row incorrectly marked full: {object_id}")
            if not markdown_path or not markdown_path.exists():
                errors.append(f"partial Markdown missing: {object_id}")
        elif markdown_path:
            errors.append(f"{status} row must not reference Markdown: {object_id}")

    for path in (root / "articles").glob("*.md"):
        if path.resolve() not in referenced_markdown:
            errors.append(f"orphan full Markdown: {path}")
    for path in (root / "articles" / "raw_html").glob("*.html"):
        if path.resolve() not in referenced_html:
            errors.append(f"orphan raw HTML: {path}")

    for (object_id, source_url), record in assets.items():
        if record.get("status") != "ok":
            continue
        local_file = str(record.get("local_file") or "")
        asset_path = root / "articles" / local_file.replace("\\", "/") if local_file else None
        if not asset_path or not asset_path.exists():
            errors.append(f"asset file missing: {object_id} {source_url}")
            continue
        expected_hash = str(record.get("sha256") or "")
        if expected_hash and sha256_file(asset_path) != expected_hash:
            errors.append(f"asset hash mismatch: {object_id} {source_url}")

    return errors, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default="export_state/manifest.csv")
    parser.add_argument("--assets", default="export_state/assets.jsonl")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        errors, counts = verify(root, root / args.manifest, root / args.assets)
    except (OSError, ValueError) as exc:
        print(f"verification error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"counts": counts, "errors": len(errors)}, ensure_ascii=False, indent=2))
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
