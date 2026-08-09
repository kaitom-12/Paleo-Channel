#!/usr/bin/env python3
"""Search local D-Lab Paleo articles and video-chapter reference notes."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_LIMIT = 10


@dataclass
class SearchResult:
    score: float
    date: datetime
    title: str
    url: str
    file: Path
    snippet: str
    note_kind: str
    reliability: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search local D-Lab Paleo articles and video chapters."
    )
    parser.add_argument("terms", nargs="+", help="Search term(s). Multiple terms are OR-ranked.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum results to print.")
    parser.add_argument("--from-date", dest="from_date", help="Earliest note date, YYYY-MM-DD.")
    parser.add_argument("--to-date", dest="to_date", help="Latest note date, YYYY-MM-DD.")
    parser.add_argument(
        "--source",
        choices=["all", "articles", "video-chapters"],
        default="all",
        help="Source notes to search. Video chapters are non-verbatim reference summaries.",
    )
    parser.add_argument("--root", default=".", help="Paleo Channel repository root.")
    return parser.parse_args()


def parse_date(value: str) -> datetime:
    value = (value or "").strip()
    for fmt, length in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(value[:length], fmt)
        except ValueError:
            continue
    return datetime.min


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def load_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_sources(root: Path, source: str) -> list[tuple[str, dict[str, str]]]:
    rows: list[tuple[str, dict[str, str]]] = []
    if source in {"all", "articles"}:
        article_manifest = root / "export_state" / "manifest.csv"
        rows.extend(("article", row) for row in load_manifest(article_manifest))
    if source in {"all", "video-chapters"}:
        video_manifest = root / "export_state" / "video_chapters_manifest.csv"
        if video_manifest.exists():
            rows.extend(("video_chapter", row) for row in load_manifest(video_manifest))
        elif source == "video-chapters":
            raise FileNotFoundError(f"manifest not found: {video_manifest}")
    return rows


def read_note(root: Path, file_value: str) -> str:
    path = root / file_value.replace("\\", "/")
    return path.read_text(encoding="utf-8", errors="replace")


def extract_headings(text: str) -> str:
    headings = []
    for line in text.splitlines():
        if line.startswith("#"):
            headings.append(line.lstrip("#").strip())
    return " ".join(headings)


def build_snippet(text: str, terms: list[str], width: int = 130) -> str:
    body = re.sub(r"^---.*?---", "", text, count=1, flags=re.S)
    body = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)
    flat = normalize(body)
    folded = fold(flat)
    folded_terms = [fold(term) for term in terms]
    positions = [folded.find(term) for term in folded_terms if term and folded.find(term) >= 0]
    if not positions:
        return flat[:width] + ("..." if len(flat) > width else "")
    pos = min(positions)
    start = max(pos - width // 3, 0)
    snippet = flat[start : start + width].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if start + width < len(flat) else ""
    return f"{prefix}{snippet}{suffix}"


def score_text(title: str, headings: str, body: str, terms: list[str]) -> float:
    title_l = fold(title)
    headings_l = fold(headings)
    body_l = fold(body)
    score = 0.0
    for term in terms:
        t = fold(term)
        if not t:
            continue
        score += title_l.count(t) * 10
        score += headings_l.count(t) * 6
        score += body_l.count(t)
        if t in title_l:
            score += 8
        if t in headings_l:
            score += 4
    if len(terms) > 1 and all(term.lower() in body_l for term in terms):
        score += 8
    return score


def in_date_range(date: datetime, from_date: datetime | None, to_date: datetime | None) -> bool:
    if from_date and date < from_date:
        return False
    if to_date and date > to_date:
        return False
    return True


def search(
    root: Path,
    terms: list[str],
    limit: int,
    from_date: datetime | None,
    to_date: datetime | None,
    source: str = "all",
) -> list[SearchResult]:
    rows = load_sources(root, source)
    results: list[SearchResult] = []
    for note_kind, row in rows:
        if note_kind == "article":
            if (row.get("origin") or "").strip():
                continue
            if (row.get("source_type") or "").strip() not in {"", "Dラボ内記事"}:
                continue
        date = parse_date(row.get("date", ""))
        if not in_date_range(date, from_date, to_date):
            continue
        file_value = row.get("file", "")
        if not file_value:
            continue
        try:
            text = read_note(root, file_value)
        except OSError:
            continue
        title = row.get("title") or ""
        headings = extract_headings(text)
        score = score_text(title, headings, text, terms)
        if note_kind == "video_chapter":
            score *= 0.6
        if score <= 0:
            continue
        results.append(
            SearchResult(
                score=score,
                date=date,
                title=title,
                url=row.get("url") or "",
                file=(root / file_value.replace("\\", "/")).resolve(),
                snippet=build_snippet(text, terms),
                note_kind=note_kind,
                reliability=(
                    row.get("reliability") or "reference_only"
                    if note_kind == "video_chapter"
                    else "article"
                ),
            )
        )
    results.sort(key=lambda item: (item.score, item.date), reverse=True)
    return results[:limit]


def print_results(results: list[SearchResult]) -> None:
    if not results:
        print("No local Paleo notes matched.")
        return
    for idx, item in enumerate(results, 1):
        date = item.date.strftime("%Y-%m-%d") if item.date != datetime.min else "unknown-date"
        source_label = "動画チャプター・参考／非逐語" if item.note_kind == "video_chapter" else "記事"
        print(f"{idx}. [{date}] [{source_label}] score={item.score:.1f} {item.title}")
        print(f"   url: {item.url}")
        print(f"   file: {item.file}")
        print(f"   snippet: {item.snippet}")
        print()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    from_date = parse_date(args.from_date) if args.from_date else None
    to_date = parse_date(args.to_date) if args.to_date else None
    terms = [normalize(term) for term in args.terms if normalize(term)]
    if not terms:
        print("At least one search term is required.", file=sys.stderr)
        return 2
    results = search(root, terms, max(args.limit, 1), from_date, to_date, args.source)
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
