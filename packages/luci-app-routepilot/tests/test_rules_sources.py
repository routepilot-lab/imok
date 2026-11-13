import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from rules_sources import presets


class RulesSourceCatalogTests(unittest.TestCase):
    def test_catalog_has_unique_https_credential_free_choices(self):
        rows = presets()
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual(len({row["url"] for row in rows}), len(rows))
        for row in rows:
            with self.subTest(id=row["id"]):
                self.assertEqual(set(row), {"id", "label", "kinds", "url", "project", "mirror"})
                parsed = urlsplit(row["url"])
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(parsed.hostname)
                self.assertIsNone(parsed.username)
                self.assertIsNone(parsed.password)
                self.assertFalse(parsed.query or parsed.fragment)
                self.assertTrue(row["kinds"])
                self.assertTrue(set(row["kinds"]) <= {"china", "china_domains", "gfw", "whitelist"})
                self.assertIs(type(row["mirror"]), bool)

    def test_categories_offer_compatible_choices_without_claiming_mirror_independence(self):
        rows = presets()
        china = [row for row in rows if "china" in row["kinds"] and not row["mirror"]]
        self.assertGreaterEqual(len({row["project"] for row in china}), 3)
        for kind in ["china_domains", "whitelist", "gfw"]:
            choices = [row for row in rows if kind in row["kinds"]]
            self.assertGreaterEqual(len(choices), 2)
            canonical = [row for row in choices if not row["mirror"]]
            self.assertTrue(canonical)
            for mirror in [row for row in choices if row["mirror"]]:
                self.assertIn(mirror["project"], {row["project"] for row in canonical})
                self.assertIn("分发镜像", mirror["label"])
        self.assertTrue(all(row["url"].endswith(".txt") for row in rows))

    def test_callers_cannot_change_subsequent_catalog_or_its_kinds(self):
        a = presets()
        a[0]["url"] = "https://example.invalid/changed"
        a[0]["kinds"].append("gfw")
        a.pop()
        b = presets()
        self.assertNotEqual(a[0]["url"], b[0]["url"])
        self.assertEqual(b[0]["kinds"], ["china"])
        self.assertEqual(len(b), 10)
