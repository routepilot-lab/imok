# Synthetic unit-test addresses; no live router, tenant or user configuration.
import copy
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'root/usr/lib/routepilot'))
import exits
import masque
import masque_config
import mplane

IDS = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
       '33333333-3333-4333-8333-333333333333', 'abcdefab-abcd-4abc-8abc-abcdefabcdef']

def directory(ids=IDS):
    return 'Currently selected: ' + IDS[0] + '\n\nVirtual Networks:\n' + ''.join(
        '\n  ID: %s\n  Name: %s\n  Comment: note\n  Default: %s\n' %
        (ident, '同名网络' if i < 2 else '任意地区 '+str(i), 'true' if i == 3 else 'false') for i, ident in enumerate(ids))

class ExitTests(unittest.TestCase):
    def test_all_networks_are_returned_without_region_name_matching(self):
        items = exits.parse_networks(directory())
        self.assertEqual([n['id'] for n in items], IDS)
        self.assertEqual(items[0]['name'], items[1]['name'])
        self.assertTrue(items[-1]['is_default'])
        self.assertFalse(any('Comment' in n for n in items))
        self.assertEqual(exits.parse_networks(directory([])), [])
        self.assertEqual(exits.parse_networks(directory([IDS[-1].upper()]))[0]['id'], IDS[-1])

    def test_bad_partial_duplicate_or_oversize_directory_fails_as_a_whole(self):
        bad = [directory().replace(IDS[1], 'bad'), directory().replace('Default: false', 'Default: unknown', 1),
               directory([IDS[0], IDS[0]]), directory() + '\n-- more --', directory().replace('Virtual Networks:', 'Other:'),
               directory().rsplit('Default:', 1)[0], directory().replace('Name:', 'Label:', 1), 'x'*1048577]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                exits.parse_networks(raw)
        with patch.object(exits, 'MAX_NETWORKS', 2), self.assertRaises(ValueError):
            exits.parse_networks(directory())

    def test_legacy_and_arbitrary_uuid_resolve_without_mutating_config(self):
        for key in ['default', 'seoul', 'usa']:
            main = {'exit': key, 'vnet_'+key: IDS[-1].upper()}
            before = copy.deepcopy(main)
            self.assertEqual(masque_config.resolve_exit(main), IDS[-1])
            self.assertEqual(main, before)
        self.assertEqual(masque_config.resolve_exit({'vnet_seoul':IDS[-1]}),IDS[-1])
        self.assertEqual(masque_config.resolve_exit({'exit': IDS[-1].upper()}), IDS[-1])
        for main in [{'exit':''}, {'exit':'default'}, {'exit':'bad'}, {'exit':'usa','vnet_usa':'broken'}]:
            with self.assertRaises(ValueError):
                masque_config.resolve_exit(main)

    def test_uuid_compiles_with_same_policy_as_legacy_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'china.txt').write_text('1.0.1.0/24\n')
            cfg={'main':{'exit':'usa','vnet_usa':IDS[-1]},'policies':[{'id':'one','enabled':'1','ip':'10.77.50.100','mac':'02:00:00:00:00:01','mode':'global'}]}
            old=masque_config.compile_config(cfg, Path(folder))
            cfg['main']['exit']=IDS[-1]
            new=masque_config.compile_config(cfg, Path(folder))
            self.assertEqual(old['vnet'],new['vnet'])
            self.assertEqual(old['nft'],new['nft'])

    def test_read_action_has_no_uci_or_selection_side_effect(self):
        child=Mock(returncode=0); child.communicate.return_value=(directory(),'')
        with patch.object(exits.subprocess,'Popen',return_value=child) as popen, patch.object(masque,'command') as command, patch('sys.stdout',new_callable=io.StringIO) as out:
            masque.control('discover')
            self.assertEqual(len(json.loads(out.getvalue())['networks']),4)
            command.assert_not_called()
        args=popen.call_args.args[0]
        self.assertEqual(args[-2:],['vnet','--no-paginate'])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.kwargs['env']['LC_ALL'],'C')

    def test_timeout_kills_query_process_group_and_reports_no_partial_result(self):
        child=Mock(pid=1234);child.communicate.side_effect=[subprocess.TimeoutExpired('cli',8),('partial','')]
        with patch.object(exits.subprocess,'Popen',return_value=child),patch.object(exits.os,'killpg') as kill:
            with self.assertRaises(RuntimeError):exits.read_networks()
            kill.assert_called_once_with(1234,signal.SIGKILL)
        self.assertEqual(child.communicate.call_count,2)

    def test_missing_uuid_does_not_issue_switch_or_connect(self):
        with patch.object(mplane,'cli',return_value=Mock(stdout=directory())) as cli:
            with self.assertRaises(ValueError):mplane.Plane().connect('55555555-5555-4555-8555-555555555555')
        self.assertEqual(cli.call_count,1)
        self.assertEqual(cli.call_args.args,('vnet','--no-paginate'))

    def test_read_acl_is_separate_from_mutating_executable_and_top_tabs_cover_eight_pages(self):
        acl=json.loads((ROOT/'root/usr/share/rpcd/acl.d/luci-app-routepilot.json').read_text())['luci-app-routepilot']
        self.assertEqual(acl['read']['file']['/usr/libexec/routepilot-exits'],['exec'])
        self.assertNotIn('/usr/libexec/routepilot',acl['read']['file'])
        menu=json.loads((ROOT/'root/usr/share/luci/menu.d/luci-app-routepilot.json').read_text())
        children=[k.rsplit('/',1)[1] for k in menu if k.count('/')==3]
        self.assertEqual(set(children),{'overview','basic','devices','dns','rules','advanced','diagnostics','backup','workers','cloudflare','clients'})
