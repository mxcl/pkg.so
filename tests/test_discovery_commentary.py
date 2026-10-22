import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import discovery_commentary as c


class CommentaryTests(unittest.TestCase):
    def test_research_cache_shared_shelves_expiry_and_version_change(self):
        item = {"packageKey": "brew:python@3.15", "version": "3.15.0", "summary": "Python"}
        note = {"text": "Python’s new release adds features that may explain increased interest in the language.",
                "sources": [{"title": "Release notes", "url": "https://python.org/releases/"}]}
        today = dt.date(2026, 10, 22)
        def document():
            return {"trending": [dict(item)], "independentTaps": [dict(item)]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, "research", return_value={item["packageKey"]: note}) as research:
            path = Path(tmp) / "notes.json"
            data = document()
            c.enrich(data, refresh=True, path=path, today=today)
            research.assert_called_once()
            self.assertEqual(len(research.call_args.args[0]), 1)
            self.assertEqual(data["trending"][0]["commentary"], data["independentTaps"][0]["commentary"])
            c.enrich(document(), refresh=True, path=path, today=today)
            research.assert_called_once()
            stale = document()
            c.enrich(stale, path=path, today=today + dt.timedelta(days=7))
            self.assertNotIn("commentary", stale["trending"][0])
            item["version"] = "3.15.1"
            changed = document()
            c.enrich(changed, path=path, today=today)
            self.assertNotIn("commentary", changed["trending"][0])

    def test_invalid_sources_rejected(self):
        note = {"text": "A sufficiently long explanation of this package and its possible appeal.",
                "sources": [{"title": "Source", "url": "javascript:alert(1)"}]}
        self.assertFalse(c.valid_note(note))
        note["sources"][0]["url"] = "https://example.com/notes"
        self.assertTrue(c.valid_note(note))
        note["sources"] = []
        self.assertFalse(c.valid_note(note))

    def test_commentary_is_one_short_sentence_with_version_numbers(self):
        note = {"text": "Python 3.15 adds lazy imports and immutable dictionaries, which may be driving interest in the new release.",
                "sources": [{"title": "Release notes", "url": "https://python.org/releases/"}]}
        self.assertTrue(c.valid_note(note))
        for text in (
            "Python adds lazy imports. These features may be driving interest.",
            "Why is Python trending? Its release adds useful features.",
            "A useful language! Its release adds features for developers.",
            "Python adds lazy imports\nwhich may be driving interest in the release.",
            "word " * 36,
            "x" * 281,
        ):
            with self.subTest(text=text):
                self.assertFalse(c.valid_note({**note, "text": text}))

    def test_failed_research_preserves_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.json"
            path.write_text('{}')
            with patch.object(c, "research", side_effect=ValueError("bad response")):
                with self.assertRaises(ValueError):
                    c.enrich({"trending": [{"packageKey": "brew:tool"}]}, refresh=True, path=path)
            self.assertEqual(json.loads(path.read_text()), {})
