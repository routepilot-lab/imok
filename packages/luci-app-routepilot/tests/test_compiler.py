"""Current shared rule-reader behavior, independently from retired backends."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import compiler as c


class RuleReaderTest(unittest.TestCase):
    def test_domains_normalize_deduplicate_and_ignore_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p / "list.txt").write_text("# comment\nExample.COM.\nexample.com\napi.example.org\n")
            self.assertEqual(
                c.read_rules(p, "list.txt", "domain"),
                ["domain:example.com", "domain:api.example.org"],
            )

    def test_bad_country_ranges_never_become_capture_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            for bad in ["0.0.0.0/0", "192.168.0.0/16", "2001:db8::/32", "1.1.1.1/24"]:
                (p / "list.txt").write_text(bad)
                with self.assertRaises(c.ConfigError):
                    c.read_rules(p, "list.txt", "cidr")

    def test_missing_file_is_empty_and_public_ranges_remain_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            self.assertEqual(c.read_rules(p, "missing.txt", "cidr"), [])
            (p / "list.txt").write_text("1.1.8.0/24\n1.1.8.0/24\n223.5.5.0/24\n")
            self.assertEqual(c.read_rules(p, "list.txt", "cidr"), ["1.1.8.0/24", "223.5.5.0/24"])


if __name__ == "__main__":
    unittest.main()
