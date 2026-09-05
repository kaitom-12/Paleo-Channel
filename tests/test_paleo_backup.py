import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


exporter = load_module("export_paleo_markdown", "scripts/export_paleo_markdown.py")
searcher = load_module("search_paleo_notes", "scripts/search_paleo_notes.py")
verifier = load_module("verify_paleo_backup", "scripts/verify_paleo_backup.py")


class PaleoBackupTests(unittest.TestCase):
    def article(self, object_id, title, body, origin=None):
        return exporter.Article(
            object_id=object_id,
            title=title,
            body=body,
            regist_ms=1710000000000,
            channel_owner="paleo",
            origin=origin,
            thumbnail_url="https://cdn.example.test/thumb.jpg",
        )

    def test_markdown_preserves_controls_and_localizes_images(self):
        body = (
            '<h2>見出し</h2><p>本文</p>'
            '<p><img src="https://cdn.example.test/body.jpg" alt="図"></p>'
            '<table><tbody><tr><td><img src="https://cdn.example.test/table.jpg"></td></tr></tbody></table>'
            '<iframe src="https://www.youtube.com/embed/example"></iframe>'
            '<input type="checkbox" checked>'
        )
        markdown = exporter.source_body_to_markdown(
            body,
            image_url_map={
                "https://cdn.example.test/body.jpg": "assets/1/000.jpg",
                "https://cdn.example.test/table.jpg": "assets/1/001.jpg",
            },
        )
        self.assertIn("assets/1/000.jpg", markdown)
        self.assertIn("assets/1/001.jpg", markdown)
        self.assertIn("<iframe", markdown)
        self.assertIn("<input", markdown)
        self.assertIn("<table", markdown)

    def test_image_extraction_excludes_thumbnail_and_reads_srcset(self):
        body = (
            '<img src="https://cdn.example.test/thumb.jpg">'
            '<img src="/body.jpg">'
            '<img srcset="/wide.jpg 2x, /body.jpg 1x">'
        )
        urls = exporter.extract_body_image_urls(
            body,
            "https://daigovideolab.jp/blog/1",
            "https://cdn.example.test/thumb.jpg",
        )
        self.assertEqual(
            urls,
            [
                "https://daigovideolab.jp/body.jpg",
                "https://daigovideolab.jp/wide.jpg",
            ],
        )

    def test_authenticated_cache_rejects_empty_and_id_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bodies.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"object_id": "1", "status": "ok", "html": "<p>ok</p>"}),
                        json.dumps({"object_id": "2", "page_id": "9", "status": "ok", "html": "<p>bad</p>"}),
                        json.dumps({"object_id": "3", "status": "ok", "html": ""}),
                    ]
                ),
                encoding="utf-8",
            )
            records = exporter.load_authenticated_body_records(path)
            self.assertEqual(set(records), {"1"})
            self.assertEqual(records["1"]["html_sha256"], exporter.sha256_text("<p>ok</p>"))

    def test_write_outputs_separates_full_partial_pending_failed_and_blog_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_dir = root / "articles"
            incomplete_dir = root / "articles_incomplete"
            raw_dir = out_dir / "raw_html"
            state_dir = root / "export_state"
            full = self.article("1", "全文記事", "<p>索引</p>")
            partial = self.article("2", "断片記事", "<p>索引断片</p>")
            pending = self.article("3", "未取得記事", "")
            failed = self.article("4", "失敗記事", "")
            blog = self.article("5", "ブログ由来", "<p>blog</p>", origin="https://example.test")
            full_html = '<h2>全文</h2><p><img src="https://cdn.example.test/body.jpg">本文</p>'
            asset_path = out_dir / "assets" / "1" / "000.jpg"
            asset_path.parent.mkdir(parents=True)
            asset_path.write_bytes(b"image")
            progress = exporter.write_outputs(
                [full, partial, pending, failed, blog],
                out_dir,
                state_dir,
                overwrite=True,
                origin_null_only=True,
                authenticated_records={"1": {"status": "ok", "html": full_html}},
                asset_records={
                    "1": {
                        "https://cdn.example.test/body.jpg": {
                            "status": "ok",
                            "local_file": "assets/1/000.jpg",
                        }
                    }
                },
                collection_failures={"4": {"status": "failed", "reason": "timeout"}},
                incomplete_dir=incomplete_dir,
                raw_html_dir=raw_dir,
            )
            self.assertEqual(progress["full"], 1)
            self.assertEqual(progress["partial"], 1)
            self.assertEqual(progress["pending"], 1)
            self.assertEqual(progress["failed"], 1)
            self.assertEqual(progress["skipped"], 1)
            self.assertEqual(len(list(out_dir.glob("*.md"))), 1)
            self.assertEqual(len(list(incomplete_dir.glob("*.md"))), 1)
            raw_path = raw_dir / "1.html"
            self.assertEqual(raw_path.read_text(encoding="utf-8"), full_html)

            with (state_dir / "manifest.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = {row["object_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(rows["1"]["collection_status"], "full")
            self.assertEqual(rows["2"]["collection_status"], "partial")
            self.assertEqual(rows["3"]["collection_status"], "pending")
            self.assertEqual(rows["4"]["collection_status"], "failed")
            self.assertNotIn("記事サムネイル", (out_dir / rows["1"]["file"]).read_text(encoding="utf-8"))

            rerun = exporter.write_outputs(
                [full, partial, pending, failed, blog],
                out_dir,
                state_dir,
                overwrite=False,
                origin_null_only=True,
                authenticated_records={"1": {"status": "ok", "html": full_html}},
                asset_records={
                    "1": {
                        "https://cdn.example.test/body.jpg": {
                            "status": "ok",
                            "local_file": "assets/1/000.jpg",
                        }
                    }
                },
                collection_failures={"4": {"status": "failed", "reason": "timeout"}},
                incomplete_dir=incomplete_dir,
                raw_html_dir=raw_dir,
            )
            self.assertEqual(rerun["full"], 1)
            self.assertEqual(len(list(out_dir.glob("*.md"))), 1)
            self.assertEqual(len(list(incomplete_dir.glob("*.md"))), 1)

            (state_dir / "assets.jsonl").write_text(
                json.dumps(
                    {
                        "object_id": "1",
                        "source_url": "https://cdn.example.test/body.jpg",
                        "status": "ok",
                        "local_file": "assets/1/000.jpg",
                        "sha256": exporter.hashlib.sha256(b"image").hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            errors, counts = verifier.verify(
                root,
                state_dir / "manifest.csv",
                state_dir / "assets.jsonl",
            )
            self.assertEqual(errors, [])
            self.assertEqual(counts["full"], 1)

            local_records = exporter.load_records_from_manifest(state_dir / "manifest.csv")
            self.assertEqual(len(local_records), 4)
            self.assertEqual(local_records[0].object_id, "1")

    def test_search_excludes_incomplete_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "export_state").mkdir()
            (root / "articles").mkdir()
            (root / "articles_incomplete").mkdir()
            full_path = root / "articles" / "full.md"
            partial_path = root / "articles_incomplete" / "partial.md"
            full_path.write_text("# 全文\n健康と睡眠", encoding="utf-8")
            partial_path.write_text("# 断片\n健康と睡眠", encoding="utf-8")
            rows = [
                {
                    "object_id": "1", "title": "全文", "date": "2024-01-01",
                    "url": "https://daigovideolab.jp/blog/1", "file": str(full_path),
                    "origin": "", "source_type": "Dラボ内記事", "collection_status": "full",
                },
                {
                    "object_id": "2", "title": "断片", "date": "2024-01-02",
                    "url": "https://daigovideolab.jp/blog/2", "file": str(partial_path),
                    "origin": "", "source_type": "Dラボ内記事", "collection_status": "partial",
                },
            ]
            with (root / "export_state" / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            default_results = searcher.search(root, ["健康"], 10, None, None, "articles")
            all_results = searcher.search(root, ["健康"], 10, None, None, "articles", include_incomplete=True)
            self.assertEqual([result.title for result in default_results], ["全文"])
            self.assertEqual({result.title for result in all_results}, {"全文", "断片"})

    def test_fetch_all_rejects_short_algolia_page(self):
        latest = {"hits": [{"objectID": "1", "blog_regist_time": 1000}]}
        short_page = {"hits": [{"objectID": "1", "blog_regist_time": 900}]}
        with patch.object(exporter, "query_page", side_effect=[latest, short_page]), patch.object(
            exporter, "fetch_count", return_value=2
        ):
            with self.assertRaisesRegex(RuntimeError, "page count mismatch"):
                exporter.fetch_all()


if __name__ == "__main__":
    unittest.main()
