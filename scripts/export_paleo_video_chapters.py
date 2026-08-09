#!/usr/bin/env python3
"""Export D-Lab Paleo video chapters as clearly labeled reference notes.

The chapter text is not a transcript. D-Lab currently marks many chapters as
AI-generated, so the exporter preserves the source text without upgrading it
to a verbatim quote or a verified claim.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from export_paleo_markdown import API_KEY, APP_ID, JST, date_parts, safe_filename, yaml_quote


VIDEO_INDEX = "videos_publishedAt_desc"
ALGOLIA_URL = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{VIDEO_INDEX}/query"
VIDEO_FILTERS = "channel_owner:paleo AND type:niconico AND hasBookmark:true"
FIRESTORE_API_KEY = "AIzaSyBHCjZu7tyk38tvw_XeKuDzQh1_rd-ft5o"
FIRESTORE_BASE_URL = (
    "https://firestore.googleapis.com/v1/projects/daigovideoapp-d658e/"
    "databases/(default)/documents"
)

SOURCE_TYPE = "Dラボ動画チャプター"
CONTENT_SOURCE_AI = "Dラボ動画チャプター（AI生成）"
CONTENT_SOURCE_MIXED = "Dラボ動画チャプター（生成方法不明または編集済み）"
CONTENT_COMPLETENESS = "chapter_summary"
RELIABILITY = "reference_only"


@dataclass
class Video:
    video_id: str
    title: str
    published_ms: int
    duration_seconds: int
    channel_owner: str
    categories: list[str]
    hashtags: list[str]
    thumbnail_url: str | None


def algolia_query(page: int, hits_per_page: int) -> dict:
    params = urllib.parse.urlencode(
        {
            "query": "",
            "hitsPerPage": str(hits_per_page),
            "page": str(page),
            "filters": VIDEO_FILTERS,
            "attributesToRetrieve": json.dumps(
                [
                    "id",
                    "objectID",
                    "title",
                    "publishedAt",
                    "duration",
                    "channel_owner",
                    "category",
                    "hashtag",
                    "thumbnail",
                ],
                ensure_ascii=False,
            ),
        }
    )
    request = urllib.request.Request(
        ALGOLIA_URL,
        data=json.dumps({"params": params}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Algolia-Application-Id": APP_ID,
            "X-Algolia-API-Key": API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def video_from_hit(hit: dict) -> Video:
    return Video(
        video_id=str(hit.get("id") or hit.get("objectID") or ""),
        title=str(hit.get("title") or "").strip(),
        published_ms=int(hit.get("publishedAt") or 0),
        duration_seconds=int(hit.get("duration") or 0),
        channel_owner=str(hit.get("channel_owner") or "paleo"),
        categories=as_string_list(hit.get("category")),
        hashtags=as_string_list(hit.get("hashtag")),
        thumbnail_url=str(hit.get("thumbnail") or "").strip() or None,
    )


def fetch_videos(limit: int | None = None) -> list[Video]:
    videos: list[Video] = []
    seen: set[str] = set()
    page = 0
    while True:
        data = algolia_query(page, 100)
        hits = data.get("hits", [])
        if not hits:
            break
        for hit in hits:
            video = video_from_hit(hit)
            if not video.video_id or video.video_id in seen:
                continue
            seen.add(video.video_id)
            videos.append(video)
            if limit and len(videos) >= limit:
                return videos
        page += 1
        if page >= int(data.get("nbPages") or 0):
            break
    return videos


def firestore_value(value: object) -> object:
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return str(value["stringValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "timestampValue" in value:
        return str(value["timestampValue"])
    return None


def fetch_chapters(video_id: str) -> list[dict]:
    encoded_id = urllib.parse.quote(video_id, safe="")
    base_url = f"{FIRESTORE_BASE_URL}/bookmarks/{encoded_id}/bookmarkList"
    chapters: list[dict] = []
    page_token: str | None = None
    while True:
        query = {"pageSize": "1000", "orderBy": "time", "key": FIRESTORE_API_KEY}
        if page_token:
            query["pageToken"] = page_token
        url = f"{base_url}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        for document in data.get("documents", []):
            fields = {
                key: firestore_value(value)
                for key, value in (document.get("fields") or {}).items()
            }
            text = str(fields.get("text") or "").strip()
            if not text:
                continue
            chapters.append(
                {
                    "time": int(fields.get("time") or 0),
                    "text": text,
                    "setter": str(fields.get("lastUpdateUserId") or ""),
                    "updated_at": str(fields.get("updateAt") or ""),
                }
            )
        page_token = str(data.get("nextPageToken") or "") or None
        if not page_token:
            break
    chapters.sort(key=lambda item: item["time"])
    return chapters


def format_timestamp(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def generation_kind(chapters: list[dict]) -> str:
    setters = {chapter["setter"] for chapter in chapters if chapter.get("setter")}
    if setters == {"AI_CHAPTER_SETTER"}:
        return "ai_generated"
    return "unknown_or_edited"


def content_source(generation: str) -> str:
    return CONTENT_SOURCE_AI if generation == "ai_generated" else CONTENT_SOURCE_MIXED


def build_markdown(video: Video, chapters: list[dict]) -> str:
    date_full, _ = date_parts(video.published_ms)
    url = f"https://daigovideolab.jp/play/{video.video_id}"
    generation = generation_kind(chapters)
    lines = [
        "---",
        'source: "Dラボ"',
        f"url: {yaml_quote(url)}",
        f"date: {yaml_quote(date_full)}",
        'channel: "パレオ"',
        'type: "動画チャプター"',
        f"video_id: {yaml_quote(video.video_id)}",
        f"source_type: {yaml_quote(SOURCE_TYPE)}",
        f"content_source: {yaml_quote(content_source(generation))}",
        f"content_completeness: {yaml_quote(CONTENT_COMPLETENESS)}",
        f"reliability: {yaml_quote(RELIABILITY)}",
        "verbatim: false",
        "speaker_verified: false",
        f"chapter_generation: {yaml_quote(generation)}",
        f"duration_seconds: {video.duration_seconds}",
        (
            f"thumbnail_url: {yaml_quote(video.thumbnail_url)}"
            if video.thumbnail_url
            else "thumbnail_url: null"
        ),
        "---",
        "",
        f"# {video.title}",
        "",
        "> [!WARNING]",
        "> Dラボ動画ページのチャプター要約を保存した参考資料。逐語録ではなく、AI生成を含む。",
        "> 発言の正確な引用や断定には使わず、元動画の該当時刻を確認すること。",
        "",
        f"- 公開日時: {date_full}",
        f"- 動画時間: {format_timestamp(video.duration_seconds)}",
        f"- チャプター数: {len(chapters)}",
        f"- 元動画: [{url}]({url})",
    ]
    if video.categories:
        lines.append(f"- カテゴリ: {', '.join(video.categories)}")
    if video.hashtags:
        lines.append(f"- タグ: {', '.join('#' + tag for tag in video.hashtags)}")
    if video.thumbnail_url:
        lines.extend(["", f"![動画サムネイル]({video.thumbnail_url})"])

    for index, chapter in enumerate(chapters, 1):
        chapter_lines = [line.strip() for line in chapter["text"].splitlines()]
        heading = chapter_lines[0] if chapter_lines and chapter_lines[0] else f"チャプター {index:02d}"
        heading = heading.lstrip("#").strip()
        lines.extend(["", f"## {format_timestamp(chapter['time'])} — {heading}", ""])
        body_lines = [line for line in chapter_lines[1:] if line]
        if body_lines:
            lines.extend(body_lines)
        else:
            lines.append("*要点なし*")
    lines.append("")
    return "\n".join(lines)


def reset_outputs(out_dir: Path, state_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.md"):
        path.unlink()
    for name in [
        "video_chapters_manifest.csv",
        "video_chapters_errors.csv",
        "video_chapters_progress.json",
    ]:
        path = state_dir / name
        if path.exists():
            path.unlink()


def write_outputs(
    videos: list[Video],
    out_dir: Path,
    state_dir: Path,
    overwrite: bool,
    request_delay: float,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    assigned_names: set[str] = set()
    manifest_rows: list[dict] = []
    error_rows: list[dict] = []
    created = updated = unchanged = skipped = errors = 0

    for index, video in enumerate(videos, 1):
        try:
            chapters = fetch_chapters(video.video_id)
            if not chapters:
                skipped += 1
                error_rows.append(
                    {
                        "video_id": video.video_id,
                        "title": video.title,
                        "reason": "no_chapters",
                    }
                )
                continue
            date_full, date_compact = date_parts(video.published_ms)
            filename = safe_filename(date_compact, video.title, video.video_id, assigned_names)
            path = out_dir / filename
            markdown = build_markdown(video, chapters)
            if path.exists():
                current = path.read_text(encoding="utf-8", errors="replace")
                if current == markdown:
                    unchanged += 1
                elif overwrite:
                    path.write_text(markdown, encoding="utf-8", newline="\n")
                    updated += 1
                else:
                    skipped += 1
            else:
                path.write_text(markdown, encoding="utf-8", newline="\n")
                created += 1

            generation = generation_kind(chapters)
            manifest_rows.append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "date": date_full,
                    "url": f"https://daigovideolab.jp/play/{video.video_id}",
                    "file": str(path),
                    "duration_seconds": video.duration_seconds,
                    "chapters": len(chapters),
                    "chapter_chars": sum(len(chapter["text"]) for chapter in chapters),
                    "channel_owner": video.channel_owner,
                    "source_type": SOURCE_TYPE,
                    "content_source": content_source(generation),
                    "content_completeness": CONTENT_COMPLETENESS,
                    "reliability": RELIABILITY,
                    "verbatim": "false",
                    "speaker_verified": "false",
                    "chapter_generation": generation,
                    "thumbnail_url": video.thumbnail_url or "",
                }
            )
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors += 1
            error_rows.append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "reason": f"error: {exc}",
                }
            )
        if index % 25 == 0:
            print(
                f"processed={index} created={created} updated={updated} "
                f"skipped={skipped} errors={errors}",
                flush=True,
            )
        if request_delay > 0:
            time.sleep(request_delay)

    manifest_path = state_dir / "video_chapters_manifest.csv"
    manifest_fields = [
        "video_id",
        "title",
        "date",
        "url",
        "file",
        "duration_seconds",
        "chapters",
        "chapter_chars",
        "channel_owner",
        "source_type",
        "content_source",
        "content_completeness",
        "reliability",
        "verbatim",
        "speaker_verified",
        "chapter_generation",
        "thumbnail_url",
    ]
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_fields)
        writer.writeheader()
        writer.writerows(manifest_rows)

    errors_path = state_dir / "video_chapters_errors.csv"
    with errors_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["video_id", "title", "reason"])
        writer.writeheader()
        writer.writerows(error_rows)

    progress = {
        "records": len(videos),
        "exported": len(manifest_rows),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "errors": errors,
        "manifest": str(manifest_path),
        "errors_file": str(errors_path),
        "updated_at": datetime.now(JST).isoformat(),
    }
    progress_path = state_dir / "video_chapters_progress.json"
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return progress


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export D-Lab Paleo video chapter summaries as reference notes."
    )
    parser.add_argument("--out-dir", default="video_chapters")
    parser.add_argument("--state-dir", default="export_state")
    parser.add_argument("--latest", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--reset-out-dir", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.02)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    state_dir = Path(args.state_dir)
    if args.reset_out_dir:
        reset_outputs(out_dir, state_dir)
    videos = fetch_videos(args.latest)
    print(f"fetched={len(videos)}")
    progress = write_outputs(
        videos,
        out_dir,
        state_dir,
        overwrite=args.overwrite,
        request_delay=max(args.request_delay, 0),
    )
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    return 0 if progress["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
