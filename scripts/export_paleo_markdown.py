#!/usr/bin/env python3
"""Export D-Lab Paleo articles into derivative Markdown notes.

The exporter is intentionally note-oriented and does not copy full articles.
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

SEC_HEADINGS = "## \u5168\u898b\u51fa\u3057"
SEC_CLAIM = "## \u8a18\u4e8b\u306e\u4e3b\u5f35"
SEC_DETAIL_BY_HEADING = "## \u898b\u51fa\u3057\u3054\u3068\u306e\u8a73\u7d30\u30e1\u30e2"
SEC_DETAIL = "## \u8a73\u7d30\u30e1\u30e2"
SEC_EXAMPLES = "## \u8a18\u4e8b\u5185\u306e\u4f8b\u30fb\u30c1\u30a7\u30c3\u30af\u9805\u76ee"
SEC_STEPS = "## \u5b9f\u8df5\u624b\u9806"
SEC_TERMS = "## \u7528\u8a9e\u30fb\u30d5\u30ec\u30fc\u30e0\u30ef\u30fc\u30af"
SEC_SELF = "## \u81ea\u5206\u7528\u30e1\u30e2"


@dataclass
class Article:
    object_id: str
    title: str
    body: str
    regist_ms: int
    channel_owner: str
    origin: str | None


@dataclass
class Section:
    heading: str
    blocks: list[str]


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
            ["title", "body", "blog_regist_time", "channel_owner", "origin", "objectID"],
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


def parse_sections(text: str) -> tuple[list[str], list[Section], list[str]]:
    preface: list[str] = []
    sections: list[Section] = []
    current: Section | None = None
    for block in split_blocks(text):
        if is_heading(block):
            current = Section(normalize_space(block), [])
            sections.append(current)
        elif current is None:
            preface.append(block)
        else:
            current.blocks.append(block)
    return [s.heading for s in sections], sections, preface


def split_sentences(text: str) -> list[str]:
    text = normalize_space(text)
    parts = re.split(r"(?<=[。！？!?])", text)
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def score_sentence(sentence: str) -> int:
    keywords = ["重要", "ポイント", "結論", "つまり", "理由", "研究", "結果", "具体", "例えば", "たとえば", "注意", "改善", "チェック", "方法", "効果", "問題", "必要"]
    return sum(2 for k in keywords if k in sentence) + min(len(sentence) // 80, 3)


def note_phrase(sentence: str, max_len: int = 130) -> str:
    s = normalize_space(sentence)
    s = re.sub(r"^[・\-*]\s*", "", s).strip("「」")
    s = re.sub(r"。$", "", s)
    if len(s) > max_len:
        return s[: max_len - 1].rstrip("、。,. ") + "..."
    return s


def top_notes(blocks: list[str], limit: int) -> list[str]:
    sentences: list[str] = []
    for block in blocks:
        if len(block) < 140:
            sentences.append(block)
        else:
            sentences.extend(split_sentences(block))
    ranked = sorted(enumerate(sentences), key=lambda x: (-score_sentence(x[1]), x[0]))
    notes: list[str] = []
    seen: set[str] = set()
    for _, sentence in ranked:
        phrase = note_phrase(sentence)
        key = phrase[:44]
        if len(phrase) < 18 or key in seen:
            continue
        seen.add(key)
        notes.append(phrase)
        if len(notes) >= limit:
            break
    return notes


def collect_examples(text: str, limit: int = 40) -> list[str]:
    examples: list[str] = []
    for block in split_blocks(text):
        s = normalize_space(block)
        if any(k in s for k in ["たとえば", "例えば", "チェック", "□", "→", "：", ":", "ステップ", "ポイント"]):
            phrase = note_phrase(s, 135)
            if 8 <= len(phrase) <= 140:
                examples.append(phrase)
    out: list[str] = []
    seen: set[str] = set()
    for ex in examples:
        key = ex[:45]
        if key not in seen:
            seen.add(key)
            out.append(ex)
        if len(out) >= limit:
            break
    return out


FRAMEWORKS = [
    "COM-B",
    "Theory of Constraints",
    "EPA",
    "DHA",
    "GLP-1",
    "NOVA",
    "HIIT",
    "CBT",
    "感情スキーマ",
    "ミトコンドリア",
    "細胞エネルギー",
    "体内時計",
]


def collect_terms(text: str, title: str) -> list[str]:
    haystack = f"{title}\n{text}"
    terms = [term for term in FRAMEWORKS if term in haystack]
    for term in re.findall(r"\b[A-Z][A-Za-z0-9+\-]{2,}\b", haystack):
        if term not in terms:
            terms.append(term)
    return terms[:20]


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


def build_markdown(article: Article, headings: list[str], sections: list[Section], preface: list[str]) -> str:
    cleaned = clean_body(article.body)
    date_full, _ = date_parts(article.regist_ms)
    url = f"https://daigovideolab.jp/blog/{article.object_id}"
    examples = collect_examples(cleaned)
    terms = collect_terms(cleaned, article.title)
    preface_notes = top_notes(preface or [cleaned], 7)
    source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB

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
        "---",
        "",
        f"# {article.title}",
        "",
        SEC_HEADINGS,
        "",
    ]
    lines.extend([f"- {h}" for h in headings] if headings else ["- 明確な見出し構造なし"])
    lines.extend(["", SEC_CLAIM, ""])
    lines.append(f"この記事は「{article.title}」をテーマに、本文の主張・例・実践ポイントを整理したノート。")
    if preface_notes:
        lines.append("")
        lines.extend(f"- {note}" for note in preface_notes)

    if sections:
        lines.extend(["", SEC_DETAIL_BY_HEADING, ""])
        for section in sections:
            lines.extend([f"### {section.heading}", ""])
            notes = top_notes(section.blocks, 8)
            lines.extend(f"- {note}" for note in notes) if notes else lines.append("- この見出しは短い区切りとして扱う。")
            lines.append("")
    else:
        lines.extend(["", SEC_DETAIL, ""])
        notes = top_notes([cleaned], 16)
        lines.extend(f"- {note}" for note in notes)

    lines.extend(["", SEC_EXAMPLES, ""])
    lines.extend(f"- {ex}" for ex in examples) if examples else lines.append("- 明確な例・チェック項目は少なめ。")

    lines.extend(["", SEC_STEPS, ""])
    practical = [h for h in headings if re.search(r"(ステップ|ワーク|チェック|実践|改善|判定|ポイント|方法)", h)]
    if practical:
        for idx, item in enumerate(practical[:12], 1):
            lines.append(f"{idx}. {item}を自分の生活や課題に当てはめて確認する。")
    else:
        for idx, note in enumerate(top_notes([cleaned], 6), 1):
            lines.append(f"{idx}. {note}という観点で、自分の状況を見直す。")

    lines.extend(["", SEC_TERMS, ""])
    lines.extend(f"- {term}" for term in terms) if terms else lines.append("- 主要な専門用語は本文メモ内で確認する。")

    lines.extend(["", SEC_SELF, ""])
    lines.extend(
        [
            "- 後で見返すときは、記事の主張、詳細メモ、実践手順の順に読む。",
            "- このノートは原文の代替ではなく、復習と検索のための言い換えメモとして扱う。",
            "- 判断に使う場合は、URL先で原文と研究リンクを確認する。",
            "",
        ]
    )
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


def write_outputs(records: list[Article], out_dir: Path, state_dir: Path, overwrite: bool, origin_null_only: bool) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    existing_names = {p.name for p in out_dir.glob("*.md")}
    created = skipped = errors = 0
    manifest_rows: list[dict] = []
    skip_rows: list[dict] = []

    for idx, article in enumerate(records, 1):
        try:
            source_type = SOURCE_TYPE_BLOG if article.origin else SOURCE_TYPE_DLAB
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

            cleaned = clean_body(article.body)
            headings, sections, preface = parse_sections(cleaned)
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

            markdown = build_markdown(article, headings, sections, preface)
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
        fields = ["object_id", "title", "date", "url", "file", "headings", "bytes", "origin", "source_type"]
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
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    state_dir = Path(args.state_dir)
    if args.reset_out_dir:
        reset_outputs(out_dir, state_dir)

    records = fetch_recent(args.latest) if args.latest else fetch_all(args.max_records)
    print(f"fetched={len(records)}")
    progress = write_outputs(records, out_dir, state_dir, args.overwrite, args.origin_null_only)
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
