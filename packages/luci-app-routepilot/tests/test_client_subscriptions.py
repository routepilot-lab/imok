"""Synthetic cloud transport only: stable links, publication and ownership."""

import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import client_access as access
import client_subscriptions as sub
import client_provider as provider


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        self.private.chmod(0o700)
        self.row = {}
        self.uri = "masque://162.159.197.2:443?privateKey=SYNTHETIC&ip=100.96.0.88#Test"
        self.auth = {"account": "a" * 32, "token": "synthetic-api-authorization-000000"}
        self.remote = None
        self.secrets = {}
        self.calls = []
        self.route = False
        self.domain_binding = None
        self.zone_records = []
        self.lost_fetch = False
        self.persisted = []
        self.addCleanup(patch.stopall)
        patch.object(sub, "authorization", return_value=self.auth).start()
        patch.object(
            sub,
            "BUNDLE",
            Path(__file__).resolve().parents[1]
            / "root/usr/share/routepilot/client-subscription/worker.mjs",
        ).start()
        self.mock_api = patch.object(sub, "api", side_effect=self.api).start()
        patch.object(sub, "fetch_body", side_effect=self.fetch).start()

    def persist(self):
        self.persisted.append(json.loads(json.dumps(self.row)))

    def api(self, auth, method, suffix, payload=None, module=None, missing=False):
        self.assertEqual(auth, self.auth)
        self.assertNotIn("routepilot-", suffix)
        self.calls.append((method, suffix))
        if suffix.startswith("/zones/"):
            if "/dns_records?" in suffix:
                return self.zone_records
            return {
                "name": "example.com",
                "status": "active",
                "account": {"id": self.auth["account"]},
            }
        if suffix.startswith("/workers/domains?"):
            return [self.domain_binding] if self.domain_binding else []
        if suffix == "/workers/domains":
            self.domain_binding = dict(payload, id="synthetic-domain-binding")
            return self.domain_binding
        if suffix == "/workers/subdomain":
            return {"subdomain": "synthetic-account"}
        if suffix.endswith("/settings"):
            return self.remote
        if suffix.endswith("/subdomain"):
            if method == "POST":
                self.route = True
            return {"enabled": self.route}
        if suffix.endswith("/secrets"):
            if method == "PUT":
                self.secrets[payload["name"]] = payload["text"]
                return {"name": payload["name"], "type": "secret_text"}
            return [{"name": k, "type": "secret_text"} for k in self.secrets]
        if "/secrets/" in suffix and method == "DELETE":
            self.secrets.pop(suffix.rsplit("/", 1)[1], None)
            return None
        if method == "PUT" and module:
            self.remote = {"bindings": payload["bindings"]}
            self.assertFalse(payload["observability"]["enabled"])
            self.assertNotIn("SYNTHETIC", module.decode())
            return {"id": "synthetic"}
        self.fail("Unexpected cloud route: " + method + " " + suffix)

    def fetch(self, url, expected=None):
        if self.lost_fetch:
            raise provider.ClientError("synthetic fetch timeout", uncertain=True)
        secret = self.secrets.get(sub.binding(url.rsplit("/", 1)[1]))
        if expected is None:
            self.assertIsNone(secret)
        else:
            self.assertEqual(json.loads(secret)["body"], expected)
        return True

    def publish(self, uri=None):
        sub.publish(self.private, access.read_private, self.row, uri or self.uri, self.persist)

    def test_fixed_url_changes_content_but_not_identity_or_address(self):
        self.publish()
        first = sub.private_url(self.private, access.read_private, self.row)
        secret = self.row["subscription"]["token"]
        original_revision = self.row["subscription"]["revision"]
        updated = self.uri.replace(":443?", ":8443?")
        self.assertTrue(sub.state(self.row, updated)["needs_update"])
        self.publish(updated)
        self.assertEqual(first, sub.private_url(self.private, access.read_private, self.row))
        self.assertEqual(secret, self.row["subscription"]["token"])
        self.assertNotEqual(original_revision, self.row["subscription"]["revision"])
        self.assertFalse(sub.state(self.row, updated)["needs_update"])
        payload = json.loads(self.secrets[sub.binding(secret)])
        self.assertEqual(base64.b64decode(payload["body"]).decode(), updated + "\n")
        self.assertEqual(sum(m == "PUT" and not p.endswith("/secrets") for m, p in self.calls), 1)
        self.assertEqual(
            (self.private / "subscription-publisher.json").stat().st_mode & 0o777, 0o600
        )

    def test_read_only_state_contains_no_secret_or_url(self):
        self.publish()
        public = json.dumps(sub.state(self.row, self.uri))
        self.assertNotIn(self.row["subscription"]["token"], public)
        self.assertNotIn(self.auth["token"], public)
        self.assertNotIn("SYNTHETIC", public)
        self.assertNotIn("https://", public)

    def test_http_readback_failure_never_reports_published(self):
        self.lost_fetch = True
        with self.assertRaises(provider.ClientError):
            self.publish()
        self.assertEqual(self.row["subscription"]["state"], "publishing")
        self.assertNotIn("synced_at", self.row["subscription"])
        original = self.row["subscription"]["token"]
        self.lost_fetch = False
        self.publish()
        self.assertEqual(original, self.row["subscription"]["token"])
        self.assertEqual(self.row["subscription"]["state"], "published")

    def test_foreign_worker_is_never_overwritten(self):
        self.remote = {
            "bindings": [
                {"name": "IMOK_CLIENT_OWNER", "type": "plain_text", "text": "different-owner"}
            ]
        }
        with self.assertRaises(provider.ClientError):
            self.publish()
        self.assertFalse(any(m != "GET" for m, _ in self.calls))
        self.assertEqual(self.secrets, {})

    def test_missing_original_publisher_does_not_change_live_link(self):
        self.publish()
        original = self.row["subscription"]["token"]
        (self.private / "subscription-publisher.json").unlink()
        self.calls.clear()
        with self.assertRaises(provider.ClientError):
            self.publish()
        self.assertEqual(self.row["subscription"]["token"], original)
        self.assertEqual(self.calls, [])

    def test_stop_removes_only_one_secret_and_new_subscription_has_new_link(self):
        self.publish()
        old = self.row["subscription"]["token"]
        self.secrets["S_other-client"] = "synthetic-other-client"
        sub.stop(self.private, access.read_private, self.row, self.persist)
        self.assertEqual(self.row["subscription"]["state"], "disabled")
        self.assertNotIn(sub.binding(old), self.secrets)
        self.assertEqual(self.secrets["S_other-client"], "synthetic-other-client")
        self.publish()
        self.assertNotEqual(old, self.row["subscription"]["token"])

    def test_lost_delete_response_can_be_reconciled_without_recreating_link(self):
        self.publish()
        old = self.row["subscription"]["token"]
        self.secrets.pop(sub.binding(old))
        sub.stop(self.private, access.read_private, self.row, self.persist)
        self.assertEqual(self.row["subscription"]["token"], old)
        self.assertEqual(self.row["subscription"]["state"], "disabled")
        self.assertFalse(any(m == "DELETE" for m, _ in self.calls))

    def test_owned_custom_domain_uses_same_link_for_port_updates(self):
        self.auth["zone_id"] = "b" * 32
        self.publish()
        first = sub.private_url(self.private, access.read_private, self.row)
        self.assertIn(".example.com/s/", first)
        self.publish(self.uri.replace(":443?", ":8443?"))
        self.assertEqual(first, sub.private_url(self.private, access.read_private, self.row))
        self.assertEqual(sum(m == "PUT" and p == "/workers/domains" for m, p in self.calls), 1)
        self.assertFalse(any(m != "GET" and p.startswith("/zones/") for m, p in self.calls))

    def test_existing_dns_record_is_never_replaced(self):
        self.auth["zone_id"] = "b" * 32
        self.zone_records = [{"id": "synthetic-existing-record"}]
        with self.assertRaises(provider.ClientError):
            self.publish()
        self.assertFalse(any(m == "PUT" and p == "/workers/domains" for m, p in self.calls))
        self.assertEqual(self.zone_records, [{"id": "synthetic-existing-record"}])
        self.assertEqual(self.secrets, {})

    def test_published_workers_dev_address_is_not_automatically_moved(self):
        self.publish()
        first = sub.private_url(self.private, access.read_private, self.row)
        access.atomic(self.private / "registry.json", json.dumps({"clients": [self.row]}))
        self.auth["zone_id"] = "b" * 32
        self.publish()
        self.assertEqual(first, sub.private_url(self.private, access.read_private, self.row))
        self.assertFalse(any("/domains" in p for _, p in self.calls))

    def test_delayed_readback_retries_reads_without_duplicate_upload(self):
        attempts = []

        def delayed(url, expected=None):
            attempts.append(url)
            if len(attempts) == 1:
                raise provider.ClientError("synthetic deployment delay", uncertain=True)
            return self.fetch(url, expected)

        with patch.object(sub, "fetch_body", side_effect=delayed), patch.object(sub.time, "sleep"):
            self.publish()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.row["subscription"]["state"], "published")
        self.assertEqual(sum(m == "PUT" and p.endswith("/secrets") for m, p in self.calls), 1)

    def test_unknown_but_successful_upload_is_reconciled_without_rewrite(self):
        self.publish()
        original = sub.private_url(self.private, access.read_private, self.row)
        self.row["subscription"].update(state="unknown", error="synthetic lost acknowledgement")
        self.calls.clear()
        self.publish()
        self.assertEqual(self.row["subscription"]["state"], "published")
        self.assertEqual(original, sub.private_url(self.private, access.read_private, self.row))
        self.assertFalse(any(m != "GET" for m, _ in self.calls))

    def test_never_uploaded_subscription_can_be_cancelled_without_cloud_auth(self):
        self.row["subscription"] = dict(state="prepared", token=sub.token(), error="not configured")
        with patch.object(
            sub, "authorization", side_effect=provider.ClientError("no authorization")
        ):
            sub.stop(self.private, access.read_private, self.row, self.persist)
        self.assertEqual(self.row["subscription"]["state"], "disabled")
        self.assertEqual(self.calls, [])

    def test_random_tokens_are_independent_and_bad_tokens_fail_closed(self):
        a, b = sub.token(), sub.token()
        self.assertEqual(len(a), 43)
        self.assertNotEqual(a, b)
        for bad in ("short", "../secret", a + "?x=1", a + "\n"):
            with self.assertRaises(provider.ClientError):
                sub.binding(bad)


class NetworkTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = provider.new_keys()

    def setUp(self):
        self.ident = "t.00000000-0000-4000-8000-000000000123"
        self.account = "a" * 32
        self.target = "00000000-0000-4000-8000-000000000111"
        self.reply = dict(
            id=self.ident,
            key=self.keys["public_key"],
            key_type="secp256r1",
            tunnel_type="masque",
            account=dict(id=self.account, account_type="team", organization="example-team"),
            config=dict(
                interface=dict(addresses=dict(v4="100.96.0.88")),
                peers=[
                    dict(public_key=self.keys["public_key"], endpoint=dict(v4="162.159.197.2:0"))
                ],
            ),
        )

    def call(self, **kwargs):
        return provider.client_networks(
            self.reply,
            "example-team",
            self.ident,
            self.keys["public_key"],
            "synthetic-token",
            **kwargs,
        )

    def test_exact_owned_get_and_patch_paths_without_uri_parameters(self):
        class Response(io.BytesIO):
            status = 200

        data = dict(
            success=True,
            result=dict(
                selected_virtual_network_id=self.target,
                virtual_networks=[
                    dict(
                        id=self.target,
                        name="美国",
                        is_default_network=False,
                        private_field="must-not-escape",
                    )
                ],
            ),
        )
        with patch.object(provider.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value = Response(json.dumps(data).encode())
            result = self.call()
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://api.cloudflareclient.com/v0/accounts/"
                + self.account
                + "/reg/"
                + self.ident
                + "/virtualnetworks",
            )
            self.assertEqual(request.method, "GET")
            self.assertNotIn("private_field", json.dumps(result))
            factory.return_value.open.return_value = Response(b'{"success":true,"result":null}')
            self.assertEqual(self.call(target=self.target), {"acknowledged": True})
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.method, "PATCH")
            self.assertTrue(request.full_url.endswith("/virtualnetworks/" + self.target))
            self.assertIsNone(request.data)
            self.assertNotIn("synthetic-token", request.full_url)

    def test_wrong_identity_or_team_is_rejected_before_transport(self):
        for changes in [
            dict(id="00000000-0000-4000-8000-000000000999"),
            dict(account=dict(id=self.account, account_type="team", organization="other-team")),
            dict(key="different-key"),
        ]:
            with patch.object(provider.urllib.request, "build_opener") as factory:
                original = dict(self.reply)
                self.reply.update(changes)
                with self.assertRaises(provider.ClientError):
                    self.call(target=self.target)
                factory.assert_not_called()
                self.reply = original

    def test_unknown_selected_network_and_non_boolean_default_are_rejected(self):
        for catalog in [
            dict(selected_id=self.target, networks=[]),
            dict(
                selected_id=self.target,
                networks=[dict(id=self.target, name="美国", is_default="false")],
            ),
            dict(
                selected_id=self.target,
                networks=[
                    dict(id="00000000-0000-4000-8000-000000000222", name="首尔", is_default=False)
                ],
            ),
        ]:
            with self.assertRaises(provider.ClientError):
                provider.network_catalog(catalog)


class SubscriptionFetchTransportTest(unittest.TestCase):
    def setUp(self):
        self.url = "https://imok-clients-000000000001.example.com/s/" + "A" * 43
        self.expected = "c3ludGhldGlj"

    def run_curl(self, code="200", value=None, cache=True, exit=0):
        from types import SimpleNamespace

        def run(args, **kwargs):
            self.assertEqual(args[:3], ["curl", "-q", "-4"])
            self.assertNotIn(self.url, " ".join(args))
            self.assertEqual(kwargs["input"], 'url = "' + self.url + '"\n')
            self.assertNotIn("-k", args)
            self.assertNotIn("-L", args)
            self.assertEqual(args[args.index("--proxy") + 1], "")
            content = Path(args[args.index("-o") + 1])
            headers = Path(args[args.index("-D") + 1])
            self.assertEqual(content.stat().st_mode & 0o777, 0o600)
            self.assertEqual(headers.stat().st_mode & 0o777, 0o600)
            content.write_text((self.expected if value is None else value) + "\n")
            headers.write_text(
                "HTTP/2 "
                + code
                + "\r\n"
                + ("Cache-Control: no-store, max-age=0\r\n" if cache else "")
            )
            return SimpleNamespace(returncode=exit, stdout=code)

        return run

    def test_ipv4_tls_readback_keeps_private_url_off_process_arguments(self):
        with patch.object(sub.subprocess, "run", side_effect=self.run_curl()):
            self.assertTrue(sub.fetch_body(self.url, self.expected))

    def test_bad_content_cache_tls_or_redirect_is_not_success(self):
        for options in [
            dict(value="old-content"),
            dict(cache=False),
            dict(exit=60),
            dict(code="302"),
        ]:
            with patch.object(sub.subprocess, "run", side_effect=self.run_curl(**options)):
                with self.assertRaises(provider.ClientError):
                    sub.fetch_body(self.url, self.expected)
        with patch.object(sub.subprocess, "run", side_effect=self.run_curl(code="404")):
            self.assertTrue(sub.fetch_body(self.url))

    def test_external_or_injected_fetch_url_is_rejected_before_process(self):
        for url in [
            "https://evil.example/s/" + "A" * 43,
            self.url + "?leak=1",
            self.url + "#fragment",
            self.url.replace("https:", "http:"),
        ]:
            with patch.object(sub.subprocess, "run") as run:
                with self.assertRaises(provider.ClientError):
                    sub.fetch_body(url, self.expected)
                run.assert_not_called()
