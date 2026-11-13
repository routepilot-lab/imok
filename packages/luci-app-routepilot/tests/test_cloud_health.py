import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import cloud_health as h


class HealthTest(unittest.TestCase):
    def probe(self, body=b"", code=200, curl=0):
        def run(args, **kw):
            self.assertNotIn("-k", args)
            self.assertNotIn("-L", args)
            self.assertIn("--max-filesize", args)
            return SimpleNamespace(stdout=body + b"\n" + str(code).encode(), returncode=curl)

        return h.probe("worker.example.com", run)

    def test_exact_marker_required(self):
        self.assertEqual(
            self.probe(
                json.dumps(
                    {"service": "routepilot-worker", "build": "cmliu-a8ab111-ordered-relays-v2"}
                ).encode()
            )["state"],
            "ready",
        )
        for body in [
            b"{}",
            b"<html>not our template</html>",
            b'{"service":"routepilot-worker","build":"old"}',
        ]:
            self.assertEqual(self.probe(body)["state"], "deployment_error")

    def test_runtime_account_and_quota_errors_are_not_called_dns_propagation(self):
        for code in [1101, 1102, 1103, 1027]:
            out = self.probe(("Error " + str(code) + " secret-private-stack").encode(), 500)
            self.assertEqual(out["state"], "deployment_error")
            self.assertEqual(out["health_error_code"], code)
            self.assertNotIn("secret-private-stack", json.dumps(out))

    def test_transport_failure_is_classified_without_disabling_tls(self):
        for code, name in [(6, "dns"), (60, "tls"), (28, "network")]:
            self.assertEqual(self.probe(curl=code)["health_category"], name)

    def test_template_and_unknown_api_error_codes(self):
        self.assertEqual(h.api_hint(10021)[1], "template")
        self.assertEqual(h.api_hint(99999)[1], "api")


if __name__ == "__main__":
    unittest.main()
