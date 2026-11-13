# Synthetic unit-test addresses; no live router, tenant or user configuration.
"""Exercise persistent overlays, source updates and actual routing compilation."""

import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from input_fixture import stage
import rules_update as core
from service import routing_digest
import masque_config
import rules_catalog as catalog
import rules_overrides as personal
from public_errors import ControlError


class PersonalRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rules = self.root / "rules"
        self.run = self.root / "run"
        self.rules.mkdir()
        self.run.mkdir()
        self.cfg = {
            "main": dict(
                enabled="1",
                exit="usa",
                vnet_usa="11111111-1111-4111-8111-111111111111",
                scope="devices",
                update_hours="24",
                gfw_url="https://example.com/gfw",
            ),
            "policies": [
                dict(
                    id="phone", enabled="1", mode="gfw", ip="10.77.50.100", mac="02:00:00:00:00:01"
                )
            ],
        }
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, "RULES", self.rules))
        self.stack.enter_context(patch.object(core, "RUN", self.run))
        self.stack.enter_context(patch.object(core, "read_config", side_effect=lambda: self.cfg))
        self.publish(
            {
                "china": "1.0.1.0/24\n",
                "gfw": "a.example.com\nb.example.com\n",
                "whitelist": "direct.example.com\n",
                "china_domains": "china.example.com\n",
            }
        )

    def publish(self, rows):
        return core.commit_rules(
            [(self.rules / (k + ".txt"), v) for k, v in rows.items()], dict(time=int(time.time()))
        )

    def payload(self, **changes):
        info = catalog.info()
        value = dict(
            kind="gfw",
            action="add",
            value="new.example.com",
            revision=info["current"]["revision"],
            overrides_revision=info["overrides"]["revision"],
        )
        value.update(changes)
        return value

    def edit(self, **changes):
        return catalog.edit(stage(self.run / "rules-input", self.payload(**changes)))

    def query(self, view="effective", **changes):
        info = catalog.info()
        payload = dict(
            kind="gfw",
            view=view,
            search="",
            page=1,
            page_size=100,
            revision=info["current"]["revision"],
            overrides_revision=info["overrides"]["revision"],
        )
        payload.update(changes)
        return catalog.query(stage(self.run / "rules-input", payload))

    def compiled(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["rule_overrides"] = personal.load(self.rules)
        with core.rule_snapshot() as snap:
            return masque_config.compile_config(cfg, snap)

    def test_add_and_exclude_change_actual_dns_rules_without_rewriting_subscription(self):
        with core.rule_snapshot() as snap:
            before = (snap / "gfw.txt").read_bytes()
        original_config = copy.deepcopy(self.cfg)
        self.edit(value="domain:NEW.Example.com.")
        self.edit(action="exclude", value="a.example.com")
        self.assertEqual(self.query()["entries"], ["b.example.com", "new.example.com"])
        self.assertEqual(
            self.compiled()["dns"]["policies"][0]["mode_domains"],
            ["b.example.com", "new.example.com"],
        )
        with core.rule_snapshot() as snap:
            self.assertEqual((snap / "gfw.txt").read_bytes(), before)
        self.assertEqual(self.cfg, original_config)
        self.assertEqual(personal.path(self.rules).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.query("current")["origins"]["a.example.com"], "excluded")
        self.assertEqual(self.query()["origins"]["new.example.com"], "personal")

    def test_ipv4_overlay_changes_actual_nft_set_and_custom_policy_priority_remains(self):
        self.cfg["policies"][0]["mode"] = "bypass_cn"
        self.cfg["main"]["custom_direct_ips"] = ["8.8.8.8/32"]
        self.edit(kind="china", value="8.8.4.4")
        self.edit(kind="china", action="exclude", value="1.0.1.0/24")
        nft = self.compiled()["nft"]
        self.assertIn("elements = { 8.8.4.4/32 }", nft)
        self.assertNotIn("1.0.1.0/24", nft)
        self.assertLess(nft.index("ip daddr { 8.8.8.8/32 }"), nft.index("ip daddr @china4"))

    def test_overlay_survives_update_and_source_rollback(self):
        self.edit(value="personal.example.com")
        self.edit(action="exclude", value="a.example.com")
        saved = personal.path(self.rules).read_bytes()
        self.publish({"gfw": "a.example.com\nnewsource.example.com\n"})
        self.assertEqual(self.query()["entries"], ["newsource.example.com", "personal.example.com"])
        info = catalog.info()
        token = stage(
            self.run / "rules-input",
            dict(
                revision=info["current"]["revision"],
                previous_revision=info["previous"]["revision"],
                confirm=True,
            ),
        )
        catalog.rollback(token)
        self.assertEqual(self.query()["entries"], ["b.example.com", "personal.example.com"])
        self.assertEqual(personal.path(self.rules).read_bytes(), saved)

    def test_download_update_preserves_personal_policy(self):
        self.edit(value="personal.example.com")
        self.edit(action="exclude", value="a.example.com")
        saved = personal.path(self.rules).read_bytes()
        with patch.object(
            core,
            "net",
            return_value=SimpleNamespace(stdout="a.example.com\nfromsource.example.com\n"),
        ):
            core.update_rules()
        self.assertEqual(personal.path(self.rules).read_bytes(), saved)
        self.assertEqual(
            self.query()["entries"], ["fromsource.example.com", "personal.example.com"]
        )

    def test_query_and_edit_bind_both_versions_and_claim_once(self):
        stale = self.payload(value="stale.example.com")
        self.edit(value="first.example.com")
        with self.assertRaises(ControlError):
            catalog.edit(stage(self.run / "rules-input", stale))
        info = catalog.info()
        q = dict(
            kind="gfw",
            view="effective",
            revision=info["current"]["revision"],
            overrides_revision=stale["overrides_revision"],
        )
        with self.assertRaises(ControlError):
            catalog.query(stage(self.run / "rules-input", q))
        token = stage(self.run / "rules-input", self.payload(value="once.example.com"))
        catalog.edit(token)
        with self.assertRaises(ControlError):
            catalog.edit(token)
        stale = self.payload(value="oldsource.example.com")
        self.publish({"gfw": "changed.example.com\n"})
        with self.assertRaises(ControlError):
            catalog.edit(stage(self.run / "rules-input", stale))

    def test_personal_views_restore_and_remove_added_have_distinct_meaning(self):
        self.edit(value="new.example.com")
        self.edit(action="exclude", value="a.example.com")
        self.assertEqual(self.query("personal_added")["entries"], ["new.example.com"])
        self.assertEqual(self.query("personal_excluded")["entries"], ["a.example.com"])
        self.edit(action="restore", value="a.example.com")
        self.edit(action="remove_added", value="new.example.com")
        self.assertEqual(self.query()["entries"], ["a.example.com", "b.example.com"])
        self.assertEqual(
            catalog.info()["current"]["revision"], catalog.info()["effective"]["revision"]
        )

    def test_add_clears_exclusion_and_exclude_removes_personal_addition(self):
        self.edit(value="new.example.com")
        self.edit(action="exclude", value="new.example.com")
        self.assertEqual(self.query("personal_added")["entries"], [])
        self.assertEqual(self.query("personal_excluded")["entries"], ["new.example.com"])
        self.edit(value="new.example.com")
        self.assertEqual(self.query("personal_excluded")["entries"], [])

    def test_invalid_entries_capacity_and_unknown_fields_do_not_write(self):
        _before = list(self.root.iterdir())
        for changes in [
            dict(value="https://example.com/"),
            dict(value="regexp:.*"),
            dict(value="bad example.com"),
            dict(value="../secret"),
            dict(kind="../../bad"),
            dict(kind="china", value="10.77.50.0/24"),
            dict(kind="china", value="::1"),
            dict(action="flush"),
            dict(extra=True),
        ]:
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                self.edit(**changes)
        self.assertFalse(personal.path(self.rules).exists())
        with patch.object(personal, "MAX_ENTRIES", 1):
            self.edit(value="one.example.com")
            with self.assertRaises(ControlError):
                self.edit(value="two.example.com")
        self.assertEqual(self.query("personal_added")["entries"], ["one.example.com"])

    def test_active_required_category_cannot_be_emptied(self):
        self.edit(action="exclude", value="a.example.com")
        with self.assertRaises(ControlError):
            self.edit(action="exclude", value="b.example.com")
        self.assertEqual(self.compiled()["dns"]["policies"][0]["mode_domains"], ["b.example.com"])
        self.cfg["main"]["enabled"] = "0"
        self.edit(action="exclude", value="b.example.com")
        self.assertEqual(self.query()["entries"], [])

    def test_invalid_or_symlink_personal_file_is_not_silently_ignored(self):
        p = personal.path(self.rules)
        p.write_text("{broken")
        with self.assertRaises(ControlError):
            catalog.info()
        p.unlink()
        target = self.root / "other"
        target.write_text("{}")
        p.symlink_to(target)
        with self.assertRaises(ControlError):
            personal.load(self.rules)
        self.assertEqual(target.read_text(), "{}")

    def test_effective_version_matches_runtime_and_digest_changes(self):
        info = catalog.info()
        self.assertEqual(info["current"]["revision"], info["effective"]["revision"])
        cfg = copy.deepcopy(self.cfg)
        cfg["rule_overrides"] = personal.load(self.rules)
        with core.rule_snapshot() as snapshot:
            before = routing_digest(cfg, snapshot)
        self.edit(value="full:exact.example.com")
        after = catalog.info()
        self.assertNotEqual(after["current"]["revision"], after["effective"]["revision"])
        (self.run / "status.json").write_text(
            json.dumps(
                dict(
                    state="running", time=time.time(), rules_revision=after["effective"]["revision"]
                )
            )
        )
        self.assertEqual(catalog.info()["application"]["state"], "applied")
        cfg["rule_overrides"] = personal.load(self.rules)
        with core.rule_snapshot() as snapshot:
            self.assertNotEqual(before, routing_digest(cfg, snapshot))
        self.assertIn(
            "full:exact.example.com", self.compiled()["dns"]["policies"][0]["mode_domains"]
        )

    def test_source_update_operation_lock_blocks_personal_edit(self):
        with catalog.operation_lock(self.rules):
            with self.assertRaises(BlockingIOError):
                self.edit(value="busy.example.com")
        self.assertFalse(personal.path(self.rules).exists())

    def test_keep_list_covers_personal_file_without_adding_identity_to_backups(self):
        keep = (
            Path(__file__).resolve().parents[1] / "root/lib/upgrade/keep.d/routepilot"
        ).read_text()
        self.assertIn("/etc/routepilot/rule-overrides.json", keep)
        self.assertNotIn("/opt/routepilot/runtime", keep)

    def test_source_update_cannot_remove_last_effective_rule_after_exclusions(self):
        self.edit(action="exclude", value="a.example.com")
        original = catalog.info()["current"]["revision"]
        with patch.object(core, "net", return_value=SimpleNamespace(stdout="a.example.com\n")):
            with self.assertRaises(ControlError):
                core.update_rules()
        self.assertEqual(catalog.info()["current"]["revision"], original)
        self.assertEqual(self.compiled()["dns"]["policies"][0]["mode_domains"], ["b.example.com"])
        self.assertEqual(json.loads((self.run / "update.json").read_text())["state"], "failed")

    def test_source_rollback_cannot_publish_empty_effective_active_category(self):
        self.publish({"gfw": "a.example.com\n"})
        self.publish({"gfw": "a.example.com\nb.example.com\n"})
        self.edit(action="exclude", value="a.example.com")
        info = catalog.info()
        token = stage(
            self.run / "rules-input",
            dict(
                revision=info["current"]["revision"],
                previous_revision=info["previous"]["revision"],
                confirm=True,
            ),
        )
        with self.assertRaises(ControlError):
            catalog.rollback(token)
        self.assertEqual(self.query()["entries"], ["b.example.com"])

    def test_candidate_accept_cannot_publish_empty_effective_active_category(self):
        self.publish({"gfw": "".join("host%d.example.com\n" % i for i in range(100))})
        self.edit(action="exclude", value="host0.example.com")
        source = catalog.info()["current"]["revision"]
        with self.assertRaises(catalog.RulesDecline):
            core.commit_rules(
                [(self.rules / "gfw.txt", "host0.example.com\n")],
                dict(
                    time=int(time.time()),
                    sources_fingerprint=catalog.sources_fingerprint(self.cfg["main"]),
                ),
            )
        info = catalog.info()
        self.assertIsNotNone(info["candidate"])
        token = stage(
            self.run / "rules-input",
            dict(revision=source, candidate_revision=info["candidate"]["revision"], confirm=True),
        )
        with self.assertRaises(ControlError):
            catalog.accept(token)
        self.assertEqual(catalog.info()["current"]["revision"], source)
        self.assertEqual(self.query()["total"], 99)


class SupervisorPersonalRulesTests(unittest.TestCase):
    def fixture(self):
        from test_rules_supervisor import SupervisorRulesTests

        fixture = SupervisorRulesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from rules_schedule import Schedule

        schedule = Schedule(fixture.rules, fixture.run)
        schedule.start(fixture.cfg["main"], "ok")
        schedule.finish(fixture.cfg["main"], True)
        return fixture

    def test_real_supervisor_loads_persistent_policy_into_compile_and_status(self):
        fixture = self.fixture()
        rules = personal.empty()
        rules["gfw"]["added"] = ["personal.example.com"]
        personal.save(fixture.rules, rules)
        fixture.run_loop(1)
        self.assertEqual(
            fixture.svc.apply.call_args.args[0]["rule_overrides"]["gfw"]["added"],
            ["personal.example.com"],
        )
        running = next(row for row in fixture.events if row["state"] == "running")
        self.assertEqual(
            running["rules_revision"],
            personal.effective_revision(catalog.directory_revision(fixture.rules), rules),
        )

    def test_personal_save_triggers_recompile_without_config_nonce_or_source_change(self):
        fixture = self.fixture()
        before = (fixture.rules / "china.txt").read_bytes()
        nonce = fixture.cfg["main"]["apply_nonce"]

        def update(index):
            if index == 1:
                rules = personal.empty()
                rules["gfw"]["added"] = ["personal.example.com"]
                personal.save(fixture.rules, rules)

        fixture.run_loop(3, on_tick=update)
        self.assertEqual(fixture.svc.apply.call_count, 2)
        self.assertEqual(fixture.cfg["main"]["apply_nonce"], nonce)
        self.assertEqual((fixture.rules / "china.txt").read_bytes(), before)
        running = [row for row in fixture.events if row["state"] == "running"]
        self.assertNotEqual(running[0]["rules_revision"], running[1]["rules_revision"])
        self.assertEqual(running[1]["rules_revision"], running[2]["rules_revision"])
