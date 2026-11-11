import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import asset_versions as assets
spec = importlib.util.spec_from_file_location('routepilot_build', ROOT / 'tools/build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AssetsTests(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder)
        for name in ['htdocs', 'root']:
            shutil.copytree(ROOT / name, root / name)
        return root

    def test_module_urls_are_content_addressed_and_all_menu_routes_agree(self):
        names = assets.validate(ROOT)
        self.assertNotEqual(names['view'], 'routepilot_masque_v0271')
        self.assertNotEqual(names['feedback'], 'feedback')

    def test_helper_change_requires_new_helper_and_dependent_view_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.fixture(folder)
            old = assets.validate(root)
            _, _, _, helper, _ = assets.paths(root)
            helper.write_text(helper.read_text() + '\n// changed fixture\n')
            with self.assertRaises(ValueError):
                assets.validate(root)
            new = assets.sync(root)
            self.assertNotEqual(old['feedback'], new['feedback'])
            self.assertNotEqual(old['view'], new['view'])

    def test_stylesheet_change_changes_css_and_dependent_view_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.fixture(folder)
            old = assets.validate(root)
            _, _, text, _, _ = assets.paths(root)
            css = assets.stylesheet(root, text)
            css.write_bytes(css.read_bytes() + b'\n/* changed fixture */\n')
            with self.assertRaises(ValueError):
                assets.validate(root)
            new = assets.sync(root)
            self.assertNotEqual(old['css'], new['css'])
            self.assertNotEqual(old['view'], new['view'])
            self.assertEqual(old['feedback'], new['feedback'])

    def test_view_change_requires_new_url_but_preserves_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.fixture(folder)
            old = assets.validate(root)
            _, view, _, _, _ = assets.paths(root)
            view.write_text(view.read_text() + '\n// changed fixture\n')
            with patch.object(builder, 'ROOT', root):
                with self.assertRaises(ValueError):
                    builder.build(root / 'dist')
            new = assets.sync(root)
            self.assertNotEqual(old['view'], new['view'])
            self.assertEqual(old['feedback'], new['feedback'])

    def test_ipk_menu_resolves_exact_modules_and_install_clears_menu_index_only(self):
        with tempfile.TemporaryDirectory() as folder:
            package = builder.build(folder)
            with tarfile.open(package, 'r:gz') as outer:
                data = outer.extractfile('./data.tar.gz').read()
                control = outer.extractfile('./control.tar.gz').read()
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as inner:
                names = set(inner.getnames())
                menu = json.load(inner.extractfile('./usr/share/luci/menu.d/luci-app-routepilot.json'))
                for entry in menu.values():
                    if entry['action']['type'] == 'alias':
                        self.assertTrue(entry['hidden'])
                        self.assertEqual(entry['action']['path'],'admin/services/routepilot/dns')
                        continue
                    view = entry['action']['path']
                    self.assertIn('./www/luci-static/resources/view/' + view + '.js', names)
                expected = assets.validate(ROOT)
                self.assertIn('./www/luci-static/resources/routepilot/' + expected['feedback'] + '.js', names)
                self.assertEqual(inner.getmember('./usr/libexec/routepilot-exits').mode, 0o755)
                self.assertEqual(inner.getmember('./usr/libexec/routepilot-observe').mode, 0o755)
                self.assertIn('./usr/lib/routepilot/receive_observer.py', names)
                self.assertIn('./www/luci-static/resources/routepilot/' + expected['css'], names)
                self.assertNotIn('./www/luci-static/resources/routepilot/v0262.css', names)
                self.assertNotIn('./www/luci-static/resources/view/routepilot_masque_v0271.js', names)
            with tarfile.open(fileobj=io.BytesIO(control), mode='r:gz') as inner:
                postinst = inner.extractfile('./postinst').read().decode()
                self.assertIn('rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*.json', postinst)
                self.assertNotIn('uci set', postinst)
                conffiles = inner.extractfile('./conffiles').read().decode().splitlines()
                sdk = (ROOT / 'Makefile').read_text().split('define Package/luci-app-routepilot/conffiles', 1)[1].split('endef', 1)[0].split()
                self.assertEqual(conffiles, ['/etc/config/routepilot'])
                self.assertEqual(sdk, conffiles, 'SDK must not add private API/node files to system backup defaults')

    def test_build_generates_package_version_without_rewriting_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            root = self.fixture(temp / 'source')
            version_file = root / 'root/usr/share/routepilot/version.json'
            version_file.write_text('{"package":"fixture-only"}')
            (version_file.parent / '.DS_Store').write_bytes(b'desktop metadata')
            with patch.object(builder, 'ROOT', root):
                package = builder.build(temp / 'output')
            self.assertEqual(version_file.read_text(), '{"package":"fixture-only"}')
            with tarfile.open(package, 'r:gz') as outer:
                payload = outer.extractfile('./data.tar.gz').read()
            with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as inner:
                self.assertNotIn('./usr/share/routepilot/.DS_Store', inner.getnames())
                version = json.load(inner.extractfile('./usr/share/routepilot/version.json'))
                self.assertEqual(version['package'], builder.VERSION)

    def test_sdk_and_custom_builder_install_the_same_executables(self):
        import subprocess,re
        expected={'etc/init.d/routepilot'}|{'usr/libexec/'+p.name for p in (ROOT/'root/usr/libexec').iterdir() if p.is_file()}
        recipe=(ROOT/'Makefile').read_text().split('define Package/luci-app-routepilot/install',1)[1].split('endef',1)[0]
        declared=set(re.findall(r'\$\(1\)/([^\s]+)',next(line for line in recipe.splitlines() if 'chmod 755' in line)))
        self.assertEqual(declared,expected)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'install';shutil.copytree(ROOT/'root',target)
            for name in expected:(target/name).chmod(0o644)
            subprocess.run(['chmod','755']+[str(target/name) for name in sorted(declared)],check=True)
            package=builder.build(Path(folder)/'build')
            with tarfile.open(package,'r:gz') as outer:raw=outer.extractfile('./data.tar.gz').read()
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as payload:
                for name in expected:
                    self.assertEqual((target/name).stat().st_mode&0o777,payload.getmember('./'+name).mode)

    def test_view_crlf_input_is_normalized_before_hash_and_byte_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.fixture(folder)
            _, view, _, _, _ = assets.paths(root)
            view.write_bytes(view.read_bytes().replace(b'\n', b'\r\n'))
            with self.assertRaises(ValueError):
                assets.validate(root)
            assets.sync(root)
            _, view, _, _, _ = assets.paths(root)
            self.assertNotIn(b'\r\n', view.read_bytes())
            self.assertIn('require', view.read_text(encoding='utf-8'))

    def test_sdk_compile_checks_asset_versions_too(self):
        makefile = (ROOT / 'Makefile').read_text(encoding='utf-8')
        compile_step = makefile.split('define Build/Compile', 1)[1].split('endef', 1)[0]
        self.assertIn('python3 ./tools/asset_versions.py --check', compile_step)
