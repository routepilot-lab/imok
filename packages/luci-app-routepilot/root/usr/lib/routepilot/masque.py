"""Single supervisor for manual MASQUE / Workers selection and scoped forwarding."""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shutil
import tempfile
from contextlib import contextmanager
import subprocess
import sys
import time
import uuid

from runtime import atomic, command, read_config, uci_value, RUN, RULES, validate_rule_text
from masque_config import compile_config, TABLE, PRIORITY, normalize_domains, selected_lans, resolve_exit
from mplane import Plane, cli, net
from native import NativeDNS
from worker_backend import WorkerPlane

STOP = False
_last_event = None


def output(state, message, devices=(), **extra):
    atomic(RUN / 'status.json', json.dumps(dict(state=state, message=message, devices=list(devices),
           time=int(time.time()), **extra), ensure_ascii=False), 0o644)
    global _last_event
    event=(state,extra.get('recovering',False),extra.get('fallback'),extra.get('runtime_id'))
    if event!=_last_event:
        try:
            path=RUN/'connection-events.json'
            try:rows=json.loads(path.read_text()).get('events',[])
            except (OSError,ValueError):rows=[]
            if not isinstance(rows,list):rows=[]
            # Only fixed public state and context; never raw errors or identity.
            rows.append(dict(time=int(time.time()),state=state,recovering=bool(extra.get('recovering')),fallback=extra.get('fallback'),backend=extra.get('backend'),runtime_id=extra.get('runtime_id')))
            atomic(path,json.dumps(dict(events=rows[-40:])) ,0o644)
        except (OSError,ValueError,TypeError,AttributeError):pass
        _last_event=event


def preflight(cfg):
    if uci_value('shadowsocksr.@global[0].global_server') not in ('', 'nil'):
        raise ValueError('SSR 当前正在接管设备；本插件未改动 SSR，请先解决接管冲突')
    if uci_value('openclash.config.enable') == '1' or uci_value('passwall.@global[0].enabled') == '1':
        raise ValueError('其他分流软件正在启用，未接管任何设备')
    whole_lan = cfg['main'].get('scope', 'devices') == 'lan'
    leases = {}
    for line in Path('/tmp/dhcp.leases').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            leases[parts[2]] = parts[1].lower()
    selected = [] if whole_lan else [(p.get('ip'), p.get('mac', '').lower()) for p in cfg['policies'] if p.get('enabled') == '1' and p.get('mode') != 'direct']
    for ip, mac in selected:
        if leases.get(ip) != mac:
            raise ValueError('所选设备 IP/MAC 与 DHCP 租约不一致，请重新选择')
    legacy = (cfg['main'].get('legacy_phone_ip', ''), cfg['main'].get('legacy_phone_mac', '').lower())
    if uci_value('warp_phone.main.enabled') == '1' and selected and legacy not in selected:
        raise ValueError('原手机 WARP 正在启用；本次仅支持对原手机进行替换试用')
    owned = (RUN / 'active-owner').exists()
    if not owned:
        if command(['nft', 'list', 'table', 'inet', 'routepilot'], check=False).returncode == 0:
            raise ValueError('专用 nftables 表被占用')
        if command(['ip', '-4', 'route', 'show', 'table', str(TABLE)], check=False).stdout.strip():
            raise ValueError('专用策略路由表被占用')
        if any(line.startswith(str(PRIORITY) + ':') for line in command(['ip', '-4', 'rule', 'show']).stdout.splitlines()):
            raise ValueError('专用路由优先级被占用')


@contextmanager
def rule_snapshot():
    """Pin a committed generation until digest and compilation finish."""
    RULES.mkdir(parents=True, exist_ok=True)
    with open(RULES / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        from rules_catalog import resolve_current
        yield resolve_current(RULES)


def commit_rules(pending, meta, *, allow_decline=False, replace_all=False):
    """Stage a complete generation, then publish it with one rename."""
    RULES.mkdir(parents=True, exist_ok=True)
    with open(RULES / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = RULES / 'current'
        from rules_catalog import resolve_current
        old = resolve_current(RULES)
        generations = RULES / '.generations'
        generations.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='rules-', dir=generations))
        link = RULES / ('.current-' + uuid.uuid4().hex)
        committed = False
        try:
            if not replace_all:
                for path in old.glob('*.txt'):
                    atomic(stage / path.name, path.read_text())
            for path, text in pending:
                atomic(stage / path.name, text)
            counts = {path.stem: sum(1 for line in path.read_text().splitlines()
                      if line.strip() and not line.startswith('#')) for path in stage.glob('*.txt')}
            from rules_catalog import enrich, declines, hold_candidate, RulesDecline
            meta = enrich(stage, old, dict(meta, counts=counts))
            atomic(stage / 'updated.json', json.dumps(meta))
            drops = declines(stage, old)
            if drops and not allow_decline:
                hold_candidate(RULES, stage, meta, drops)
                raise RulesDecline('规则数量下降超过50%，整批已暂存等待确认；继续使用上一版本')
            # All files are fsynced by atomic(); persist directory entries before publish.
            fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.symlink(str(stage.relative_to(RULES)), link)
            os.replace(link, current)
            committed = True
            # Retain current and previous generations; readers hold the shared lock.
            try:
                retained = (RULES / meta['previous_path']).resolve()
                for candidate in generations.iterdir():
                    if candidate.is_dir() and candidate.resolve() not in (stage.resolve(), retained):
                        shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                pass  # A cleanup failure must not misreport an already committed update.
            return meta
        finally:
            link.unlink(missing_ok=True)
            # SIGTERM may raise immediately after rename, before committed is assigned.
            # Never delete a generation already referenced by the published pointer.
            if not committed and not (current.is_symlink() and current.resolve() == stage.resolve()):
                shutil.rmtree(stage, ignore_errors=True)


def update_rules():
    from rules_catalog import operation_lock
    with operation_lock(RULES):
        return _update_rules()


def _update_rules():
    cfg = read_config()
    job = os.environ.get('ROUTEPILOT_UPDATE_ID') or cfg['main'].get('update_nonce') or str(int(time.time()))
    labels = {'china': '中国 IP', 'gfw': '代理域名', 'whitelist': '直连域名', 'china_domains': '中国域名'}
    try:
        with rule_snapshot() as snapshot:
            old_meta = json.loads((snapshot / 'updated.json').read_text())
    except (OSError, ValueError):
        old_meta = {}
    progress = dict(state='running', request_id=job, started_at=int(time.time()), finished_at=0,
                    phase='准备下载', percent=0, last_success_at=old_meta.get('downloaded_at', old_meta.get('time', 0)),
                    counts=old_meta.get('counts', {}), ignored_regexp=old_meta.get('ignored_regexp', {}))
    def publish():
        atomic(RUN / 'update.json', json.dumps(progress, ensure_ascii=False), 0o644)
    publish()
    pending, ignored = [], {}
    sources = [(kind, cfg['main'].get(kind + '_url', '')) for kind in labels if cfg['main'].get(kind + '_url')]
    label = '规则'
    try:
        for index, (kind, url) in enumerate(sources):
            label = labels[kind]
            progress.update(phase='正在下载并校验：' + label, percent=round(index / max(1, len(sources)) * 90))
            publish()
            if not re.fullmatch(r'https://[^\s]{1,2000}', url):
                raise ValueError('更新地址必须使用 HTTPS')
            downloader = net
            if cfg['main'].get('backend','masque') == 'worker':
                from worker_backend import curl as downloader
            result = downloader(['curl', '-fLsS', '--proto', '=https', '--proto-redir', '=https',
                          '--connect-timeout', '5', '--max-time', '20', '--max-filesize', '4194304', url])
            if kind == 'china':
                text = validate_rule_text('china', result.stdout)
            else:
                text, count = normalize_domains(result.stdout)
                ignored[kind] = count
            pending.append((RULES / (kind + '.txt'), text))
        if not pending:
            raise ValueError('尚未设置规则更新地址')
        progress.update(phase='校验完成，正在替换本插件规则', percent=95)
        publish()
        now = int(time.time())
        from rules_catalog import sources_fingerprint, content
        latest_cfg = read_config()
        if sources_fingerprint(cfg['main']) != sources_fingerprint(latest_cfg['main']):
            raise ValueError('规则来源设置已变化，请重新更新')
        from rules_overrides import load as load_personal, validate_required
        with rule_snapshot() as snapshot:
            combined = content(snapshot)
            for path, text in pending:
                combined[path.stem] = text.splitlines()
            validate_required(latest_cfg, combined, load_personal(RULES))
        meta = commit_rules(pending, dict(time=now, downloaded_at=now, ignored_regexp=ignored, sources=dict(sources), sources_fingerprint=sources_fingerprint(cfg['main']), request_id=job))
        counts = meta['counts']
        progress.update(state='success', phase='更新成功，规则已保存', percent=100, finished_at=now,
                        last_success_at=now, counts=counts, ignored_regexp=ignored)
        publish()
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else '下载或连接失败'
        from rules_catalog import RulesDecline
        progress.update(state='blocked' if isinstance(error, RulesDecline) else 'failed', phase=label + '更新失败：' + reason + '；使用最后已提交版本，可重新更新',
                        finished_at=int(time.time()))
        publish()
        raise


def finish_interrupted_update():
    """Do not leave a dead worker's progress permanently running."""
    try:
        path = RUN / 'update.json'
        state = json.loads(path.read_text())
        if state.get('state') == 'running':
            state.update(state='unknown', phase='更新任务已中断，发布结果未知；保留最后已提交版本，可重新更新', finished_at=int(time.time()))
            atomic(path, json.dumps(state, ensure_ascii=False), 0o644)
    except (OSError, ValueError):
        pass


def routing_digest(cfg, directory=None):
    data = json.loads(json.dumps(cfg))
    for key in list(data['main']):
        if key in ('apply_nonce', 'update_nonce', 'update_hours') or key.endswith('_url'):
            data['main'].pop(key, None)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode())
    for path in sorted((directory if directory is not None else RULES).glob('*.txt')):
        digest.update(path.read_bytes())
    return digest.hexdigest()


class TunnelPending(RuntimeError):
    """Withdraw capture while the existing official client reconnects."""


class Service:
    def __init__(self):
        self.backend = 'masque'
        self.plane = Plane()
        self.dns = NativeDNS()
        self.compiled = None
        self.exit_ip = ''
        self.runtime_id = ''
        self.update_job = None
        self.worker_failed_nonce = None
        self.recovery_started = None
        self.recovery_connect_sent = False

    def tunnel_ready(self):
        try:
            text=cli('status',check=False).stdout
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            text=''
        if 'Status update: Connected' in text:
            self.recovery_started=None
            self.recovery_connect_sent=False
            return
        now=time.monotonic()
        if self.recovery_started is None:
            self.recovery_started=now
        if now-self.recovery_started>=20:
            self.recovery_started=None
            raise RuntimeError('WARP 恢复等待超时，正在重建本插件专用客户端')
        if 'Status update: Disconnected' in text and not self.recovery_connect_sent:
            self.recovery_connect_sent=True
            try:cli('connect',check=False)
            except (OSError,RuntimeError,subprocess.TimeoutExpired):pass
        raise TunnelPending('WARP 暂时中断，已撤回接管并直连；等待原客户端自动恢复')

    def suspend_capture(self):
        direct=self.withdraw()
        self.dns.stop()
        self.compiled=None
        self.runtime_id=''
        return direct

    def withdraw(self):
        owner = RUN / 'active-owner'
        clean = True
        if owner.exists():
            # Keep the journal until all owned capture resources are verified absent.
            for args in ([ 'nft', 'delete', 'table', 'inet', 'routepilot'],
                         ['ip', '-4', 'rule', 'del', 'pref', str(PRIORITY), 'fwmark', '0x5a000000/0xff000000', 'lookup', str(TABLE)],
                         ['ip', '-4', 'route', 'del', 'default', 'via', '10.254.239.2', 'dev', 'rp-host', 'table', str(TABLE)],
                         ['ip', '-4', 'route', 'del', 'default', 'dev', 'rp-backup', 'table', str(TABLE)]):
                try:
                    command(args, check=False)
                except Exception:
                    clean = False
            try:
                tables = json.loads(command(['nft', '-j', 'list', 'tables']).stdout)['nftables']
                rules = json.loads(command(['ip', '-j', '-4', 'rule', 'show']).stdout)
                routes = json.loads(command(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
                clean = clean and not any(t.get('table', {}).get('name') == 'routepilot' and t['table'].get('family') == 'inet' for t in tables)
                clean = clean and not any(r.get('priority') == PRIORITY for r in rules)
                clean = clean and not any(str(r.get('table')) == str(TABLE) for r in routes)
            except Exception:
                clean = False
            if clean:
                owner.unlink(missing_ok=True)
        try:
            self.dns.stop()
        except Exception:
            clean = False
        self.compiled = None
        return clean

    def stop(self, restore=True):
        self.recovery_started=None
        self.recovery_connect_sent=False
        clean = True
        if self.update_job and self.update_job.poll() is None:
            try:
                os.killpg(self.update_job.pid, signal.SIGTERM)
                self.update_job.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.update_job.pid, signal.SIGKILL)
                    self.update_job.wait(timeout=3)
                except Exception:
                    clean = False
        self.update_job = None
        finish_interrupted_update()
        try:
            clean = self.withdraw() and clean
        except Exception:
            clean = False
        try:
            self.plane.stop()  # Includes only imok-owned resources.
            if self.backend != 'worker':
                WorkerPlane().stop()
        except Exception:
            clean = False
        legacy = '无需恢复旧手机策略'
        marker = RUN / 'phone-paused'
        if marker.exists():
            legacy = '旧手机策略保持暂停'
            if restore:
                try:
                    if uci_value('warp_phone.main.enabled') == '1':
                        command(['/etc/init.d/warp-phone', 'start'])
                        legacy = '旧手机策略启动命令已成功执行'
                    else:
                        legacy = '旧手机策略已由配置关闭，无需恢复'
                    marker.unlink(missing_ok=True)
                except Exception:
                    legacy = '旧手机策略恢复失败，请检查服务'
                    clean = False
        return dict(direct=clean, legacy=legacy)

    def apply(self, cfg, compiled):
        # Validate the complete nft transaction before pausing the original phone policy.
        exists = command(['nft', 'list', 'table', 'inet', 'routepilot'], check=False).returncode == 0
        candidate = ('delete table inet routepilot\n' if exists else '') + compiled['nft']
        command(['nft', '-c', '-f', '-'], data=candidate)
        if self.backend!='worker':self.plane.protocol_preference=cfg['main'].get('warp_protocol','auto')
        self.plane.connect(compiled['vnet'])
        self.exit_ip = self.plane.probe()
        ipaddress.IPv4Address(self.exit_ip)
        expected = '' if self.backend == 'worker' else cfg['main'].get('expected_' + compiled['exit'], '')
        if expected and self.exit_ip != expected:
            raise ValueError('实际公网出口不符合所选服务器；未应用新设备策略')
        # Never change the original UCI or boot enablement. Pause its runtime only.
        taking_phone = any(d['mode'] != 'direct' and ((d.get('scope') == 'lan' and cfg['main'].get('legacy_phone_ip') and ipaddress.ip_address(cfg['main']['legacy_phone_ip']) in ipaddress.ip_network(d['ip'])) or (d['ip'] == cfg['main'].get('legacy_phone_ip') and d['mac'] == cfg['main'].get('legacy_phone_mac', '').lower())) for d in compiled['devices'])
        if taking_phone and uci_value('warp_phone.main.enabled') == '1':
            atomic(RUN / 'phone-paused', 'restore original phone service on stop\n')
            command(['/etc/init.d/warp-phone', 'stop'])
        elif not taking_phone and (RUN / 'phone-paused').exists():
            if uci_value('warp_phone.main.enabled') == '1':
                command(['/etc/init.d/warp-phone', 'start'])
            (RUN / 'phone-paused').unlink()
        old = self.compiled
        self.dns.stop()
        try:
            self.plane.forward(compiled['devices'])
            atomic(RUN / 'active-owner', 'routepilot devices and route table 51990\n')
            command(['ip', '-4', 'route', 'replace', 'default'] + (['dev', 'rp-backup'] if self.backend == 'worker' else ['via', '10.254.239.2', 'dev', 'rp-host']) + ['table', str(TABLE)])
            rules = command(['ip', '-4', 'rule', 'show']).stdout
            if not any(line.startswith(str(PRIORITY) + ':') for line in rules.splitlines()):
                command(['ip', '-4', 'rule', 'add', 'pref', str(PRIORITY), 'fwmark', '0x5a000000/0xff000000', 'lookup', str(TABLE)])
            # fw4 reload triggered by the original service may recreate its table only.
            exists = command(['nft', 'list', 'table', 'inet', 'routepilot'], check=False).returncode == 0
            command(['nft', '-f', '-'], data=('delete table inet routepilot\n' if exists else '') + compiled['nft'])
            runtime_id = uuid.uuid4().hex
            compiled['dns']['runtime_id'] = runtime_id
            if self.backend == 'worker':
                compiled['dns']['proxy_dns_override'] = 'https://dns.google/dns-query'
                for policy in compiled['dns']['policies']:
                    policy['proxy_dns_override'] = compiled['dns']['proxy_dns_override']
            atomic(RUN / 'dns.json', json.dumps(compiled['dns']), 0o600)
            self.dns.start(RUN / 'dns.json')
            time.sleep(0.5)
            if not self.dns.alive():
                raise RuntimeError('独立 DNS 未启动，已撤回设备策略')
            self.compiled = compiled
            self.runtime_id = runtime_id
            for address in sorted({d['ip'] for d in compiled['devices'] + (old['devices'] if old else [])}):
                command(['conntrack', '-D', '-s', address], check=False)
                if self.backend != 'worker':
                    net(['conntrack', '-D', '-s', address], check=False)
        except Exception:
            self.stop(restore=False)
            raise

    def health(self):
        if not self.compiled:
            return
        log = RUN / 'client.log'
        if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
            log.write_text('')
        if not self.dns.alive():
            raise RuntimeError('设备 DNS 已停止，将撤回当前线路接管并允许宽带直连')
        if self.backend != 'worker':
            self.tunnel_ready()
        self.plane.optimize()
        listing = command(['nft', '-a', 'list', 'chain', 'inet', 'fw4', 'forward']).stdout
        tag = 'routepilot-worker-device-out' if self.backend == 'worker' else 'routepilot-device-out'
        if tag not in listing or (self.backend == 'worker' and 'routepilot-worker-udp' not in listing) or (self.backend != 'worker' and 'routepilot-control-out' not in listing):
            self.plane.forward(self.compiled['devices'])
        if command(['nft', 'list', 'table', 'inet', 'routepilot'], check=False).returncode:
            command(['nft', '-f', '-'], data=self.compiled['nft'])
        if not any(line.startswith(str(PRIORITY) + ':') for line in command(['ip', '-4', 'rule', 'show']).stdout.splitlines()):
            raise RuntimeError('专用策略路由规则已丢失，将撤回 WARP 接管并允许宽带直连')
        if not command(['ip', '-4', 'route', 'show', 'table', str(TABLE)], check=False).stdout.strip():
            raise RuntimeError('专用路由已丢失，将撤回 WARP 接管并允许宽带直连')


def lan_interfaces(include_inactive=False):
    addresses = json.loads(command(['ip', '-j', '-4', 'addr', 'show']).stdout)
    wan = {v.get('dev') for v in json.loads(command(['ip', '-j', '-4', 'route', 'show', 'default']).stdout)}
    private = [ipaddress.ip_network(v) for v in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    result = []
    for item in addresses:
        name = item.get('ifname', '')
        if name in wan or name in ('lo', 'rp-host', 'CloudflareWARP') or 'POINTOPOINT' in item.get('flags', []):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name):
            continue
        nets = []
        for a in item.get('addr_info', []):
            if a.get('family') != 'inet' or a.get('scope') != 'global' or not 16 <= a.get('prefixlen', 0) <= 30:
                continue
            net = ipaddress.ip_network(str(a['local']) + '/' + str(a['prefixlen']), strict=False)
            if any(net.subnet_of(p) for p in private):
                nets.append(str(net))
        if len(set(nets)) == 1:
            result.append({'name': name, 'network': nets[0], 'up': 'UP' in item.get('flags', [])})
    if include_inactive:
        config = json.loads(command(['ubus', 'call', 'uci', 'get', '{"config":"network"}']).stdout).get('values', {})
        known = {v['name'] for v in result}
        for name, item in config.items():
            dev = item.get('device', item.get('ifname', ''))
            if item.get('.type') != 'interface' or not isinstance(dev, str) or 'wan' in name.lower() or not (dev.startswith('br-') or name in ('lan', 'guest', 'iot')) or dev in known or dev in wan or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', dev):
                continue
            address = item.get('ipaddr', '')
            if not isinstance(address, str) or not address:
                continue
            try:
                net = ipaddress.ip_network(address if '/' in address else address + '/' + str(item.get('netmask', '255.255.255.0')), strict=False)
            except ValueError:
                continue
            if net.version == 4 and 16 <= net.prefixlen <= 30 and any(net.subnet_of(p) for p in private):
                result.append({'name': dev, 'network': str(net), 'up': False})
                known.add(dev)
    return result


def control(action, request_id=None):
    if action in ('rules-info','rules-query','rules-accept','rules-rollback','rules-edit'):
        import rules_catalog
        result = rules_catalog.info() if action == 'rules-info' else getattr(rules_catalog, {'rules-query':'query','rules-accept':'accept','rules-rollback':'rollback','rules-edit':'edit'}[action])(request_id)
        print(json.dumps(result, ensure_ascii=False))
    elif action in ('settings-export','settings-import'):
        from config_backup import export,import_settings
        print(json.dumps(export() if action=='settings-export' else import_settings(request_id),ensure_ascii=False))
    elif action in ('endpoint-info','endpoint-refresh','endpoint-save','endpoint-schedule','endpoint-recover'):
        from endpoint_control import info,refresh,save,save_schedule,save_recovery
        print(json.dumps(save_schedule(request_id) if action=='endpoint-schedule' else save_recovery(request_id) if action=='endpoint-recover' else save(request_id) if action=='endpoint-save' else refresh() if action=='endpoint-refresh' else info(),ensure_ascii=False))
    elif action in ('warp-info','exit-observe','warp-org-save'):
        from warp_control import refresh,observe,save_organization
        print(json.dumps(refresh() if action=='warp-info' else save_organization(request_id) if action=='warp-org-save' else observe(),ensure_ascii=False))
    elif action == 'interfaces':
        print(json.dumps(lan_interfaces(include_inactive=True)))
    elif action in ('enable', 'disable'):
        nonce = str(time.time_ns())
        command(['uci', 'set', 'routepilot.main.enabled=' + ('1' if action == 'enable' else '0')])
        command(['uci', 'set', 'routepilot.main.preparing=0'])
        command(['uci', 'set', 'routepilot.main.apply_nonce=' + nonce])
        command(['uci', 'commit', 'routepilot'])
        print(json.dumps({'enabled': action == 'enable', 'nonce': nonce}))
    elif action == 'apply':
        command(['uci', 'commit', 'routepilot'])
        print('本插件配置已提交')
    elif action.startswith('backup-'):
        from worker_backend import import_node, start_test, require_test
        if action == 'backup-import':
            print(json.dumps(import_node(request_id), ensure_ascii=False))
        elif action == 'backup-test':
            print(json.dumps(start_test()))
        elif action in ('backup-scan','backup-scan-apply'):
            from worker_scan import start, apply_best
            print(json.dumps(start() if action=='backup-scan' else apply_best(),ensure_ascii=False))
        elif action in ('backup-enable', 'backup-disable'):
            from worker_backend import guard
            with guard():
                if action == 'backup-enable':
                    require_test()
                    if read_config()['main'].get('enabled') != '1':
                        raise ValueError('请先核对并开启 imok 总开关')
                nonce = str(time.time_ns())
                command(['uci','set','routepilot.main.backend=' + ('worker' if action == 'backup-enable' else 'masque')])
                command(['uci','set','routepilot.main.apply_nonce=' + nonce])
                command(['uci','commit','routepilot'])
                if action == 'backup-enable':
                    (RUN / 'backup-failure.json').unlink(missing_ok=True)
                print(json.dumps({'nonce':nonce,'backend':'worker' if action=='backup-enable' else 'masque'}))
        else:
            raise ValueError('未知备用操作')
    elif action.startswith('relay-'):
        from relay_pool import save, start, apply, runtime_status
        result=save(request_id) if action=='relay-save' else start() if action=='relay-test' else apply() if action=='relay-apply' else runtime_status()
        print(json.dumps(result,ensure_ascii=False))
    elif action.startswith('cloud-'):
        from cloud_worker import save, start, promote, forget
        result=save(request_id) if action=='cloud-save' else promote() if action=='cloud-promote' else forget() if action=='cloud-forget' else start(action.removeprefix('cloud-'))
        print(json.dumps(result,ensure_ascii=False))
    elif action == 'dns-test':
        from dns_diagnostics import start
        print(json.dumps(start(), ensure_ascii=False))
    elif action == 'dns-clear':
        from dns_runtime import request_clear
        print(json.dumps(request_clear(), ensure_ascii=False))
    elif action == 'test' or action.startswith('test-'):
        from diagnostics import start
        start(action.removeprefix('test-') if action != 'test' else 'all')
        print('HTTPS 连通检测已开始')
    elif action == 'prepare':
        command(['uci', 'set', 'routepilot.main.preparing=1'])
        command(['uci', 'commit', 'routepilot'])
        print('登录环境准备中，请稍候查看状态')
    elif action == 'login':
        from auth_control import login
        print(login())
    elif action == 'register':
        from auth_control import register
        print(json.dumps(register(request_id),ensure_ascii=False))
    elif action == 'discover':
        print(json.dumps(discover_exits(), ensure_ascii=False))
    else:
        raise ValueError('无效操作')


def discover_exits():
    from exits import read_networks
    return read_networks()


def main():
    global STOP
    if len(sys.argv) not in (2,3) or len(sys.argv)==3 and sys.argv[1] not in ('endpoint-save','settings-import','register','backup-import','cloud-save','relay-save','warp-org-save','endpoint-schedule','endpoint-recover','rules-query','rules-accept','rules-rollback','rules-edit') or sys.argv[1] not in ('run', 'rules-info', 'rules-query', 'rules-accept', 'rules-rollback', 'rules-edit', 'settings-export', 'settings-import', 'endpoint-info', 'endpoint-refresh', 'endpoint-save', 'endpoint-schedule', 'endpoint-recover', 'warp-info', 'exit-observe', 'warp-org-save', 'backup-import', 'backup-test', 'backup-enable', 'backup-disable', 'backup-scan', 'backup-scan-apply', 'cloud-save', 'cloud-target', 'cloud-verify', 'cloud-deploy', 'cloud-check', 'cloud-promote', 'cloud-forget', 'relay-save', 'relay-test', 'relay-apply', 'relay-status', 'interfaces', 'enable', 'disable', 'prepare', 'login', 'register', 'discover', 'apply', 'cleanup', 'update-worker', 'dns-test', 'dns-clear', 'test', 'test-google', 'test-youtube', 'test-github', 'test-taobao', 'test-baidu'):
        raise SystemExit('Usage: routepilot run|prepare|login|register')
    os.umask(0o077)
    RUN.mkdir(parents=True, exist_ok=True)
    RUN.chmod(0o755)
    from request_input import prepare_requests
    prepare_requests(RUN)
    if sys.argv[1] == 'cleanup':
        with open(RUN / 'masque.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('主进程仍在运行；未并发清理网络')
            Service().stop()
        return
    if sys.argv[1] == 'update-worker':
        def interrupted(*_):
            raise RuntimeError('update interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        try:
            update_rules()
        except Exception:
            raise SystemExit(1)
        return
    if sys.argv[1] != 'run':
        try:
            control(sys.argv[1],sys.argv[2] if len(sys.argv)==3 else None)
        except Exception as error:
            from cloud_worker import CloudError
            from relay_pool import RelayError
            from public_errors import ControlError
            if isinstance(error,(CloudError,RelayError,ControlError)):raise SystemExit(str(error))
            if isinstance(error,BlockingIOError):raise SystemExit('另一个操作正在进行，请稍后再试')
            raise SystemExit('操作未完成，请核对服务状态和登录有效期；未显示凭据')
        return
    lock = open(RUN / 'masque.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    def halt(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    from endpoint_control import Manager,stage_restored_preferences
    stage_restored_preferences()
    endpoint_manager = Manager()
    service = Service()
    service.stop(restore=False)  # Recover owned resources without undoing fault fallback.
    previous = None
    endpoint_paused = False
    pause_cleanup = {'direct':False}
    from rules_schedule import Schedule
    schedule = Schedule(rules=RULES, run=RUN)
    last_nonce, update_message = schedule.data.get('last_nonce'), ''
    update_job_id = None
    try:
        while not STOP:
            try:
                cfg = read_config()
                if not service.update_job:
                    try:
                        schedule.reconcile(cfg['main'])
                    except OSError:
                        pass  # A rule schedule write failure must not tear down a healthy tunnel.
                try:
                    from request_input import reap_requests
                    reap_requests(RUN)
                    endpoint_manager.poll()
                except Exception:pass  # Candidate refresh must never interrupt the data plane.
                enabled = cfg['main'].get('enabled') == '1'
                if enabled:
                    choices = {item['name']: item for item in lan_interfaces()}
                    selected = selected_lans(cfg['main'])
                    if any(name not in choices or not choices[name]['up'] for name in selected):
                        raise ValueError('所选 LAN 接口缺失、未启用或无唯一有效私网网段，请重新选择')
                    cfg['main']['lan_networks'] = [{'interface': name, 'network': choices[name]['network']} for name in selected]
                preparing = cfg['main'].get('preparing') == '1'
                if endpoint_manager.recover_requested(service,cfg['main']):previous=None
                if not enabled and not preparing:
                    stopped = service.stop()
                    if not stopped['direct']:
                        raise RuntimeError('关闭清理未能确认；' + stopped['legacy'])
                    output('disabled', '未接管设备；既有网络设置保持不变；' + stopped['legacy'], applied_nonce=cfg['main'].get('apply_nonce', ''))
                    previous = None
                else:
                    desired = cfg['main'].get('backend', 'masque') if enabled else 'masque'
                    if desired not in ('masque','worker'):
                        raise ValueError('线路类型无效')
                    if desired=='masque' and enabled:
                        reason=endpoint_manager.pause_reason(cfg['main'])
                        if reason:
                            if not endpoint_paused:
                                pause_cleanup=service.stop(restore=False)
                                endpoint_paused=True
                            previous=None
                            output('error',reason,paused=True,recovering=False,fallback='direct' if pause_cleanup['direct'] else 'unknown',scope=cfg['main'].get('scope','devices'),attempted_nonce=cfg['main'].get('apply_nonce',''))
                            time.sleep(3)
                            continue
                    endpoint_paused=False
                    if desired == 'worker':
                        from worker_backend import check_retry
                        if service.worker_failed_nonce == cfg['main'].get('apply_nonce', ''):
                            raise ValueError('备用线路已失败，当前保持直连；请重新检测后手动启用，或切回主线路')
                        check_retry(cfg['main'].get('apply_nonce', ''))
                    if desired != service.backend:
                        if not service.stop(restore=False)['direct']:
                            raise RuntimeError('旧线路清理未能确认；未切换备用')
                        service.plane = WorkerPlane() if desired == 'worker' else Plane()
                        service.backend = desired
                        previous = None
                    if desired!='worker':
                        from warp_control import preference
                        requested=preference(cfg['main'].get('warp_protocol','auto'))
                        if service.plane.owned and service.plane.protocol_preference!=requested:
                            if not service.stop(restore=False)['direct']:raise RuntimeError('协议重连前清理未能确认')
                            service.plane=Plane();previous=None
                        service.plane.protocol_preference=requested
                        service.plane.organization=cfg['main'].get('organization','')
                    service.plane.start()
                    try:
                        if enabled and endpoint_manager.tick(service,cfg['main']):previous=None
                    except BlockingIOError:pass
                    if enabled and service.backend!='worker' and service.recovery_started is not None:
                        service.tunnel_ready()
                    if not enabled:
                        output('preparing', '登录环境已就绪；请完成邮箱登录，再自动读取出口', applied_nonce=cfg['main'].get('apply_nonce', ''))
                        nonce = cfg['main'].get('update_nonce', '')
                        if nonce and nonce != last_nonce:
                            try:
                                schedule.start(cfg['main'], nonce)
                                vnet = resolve_exit(cfg['main'])
                                if service.backend!='worker':service.plane.protocol_preference=cfg['main'].get('warp_protocol','auto')
                                service.plane.connect(vnet)
                                update_rules()
                                schedule.finish(cfg['main'], True)
                                output('preparing', '规则更新完成；设备分流仍关闭')
                            except Exception:
                                progress = __import__('rules_schedule').read(RUN / 'update.json')
                                if progress.get('request_id') != nonce or progress.get('state') == 'running':
                                    atomic(RUN / 'update.json', json.dumps(dict(state='failed', request_id=nonce, phase='更新未开始：请确认登录环境和出口已就绪；保留原规则', started_at=int(schedule.data.get('last_attempt_at', time.time())), finished_at=int(time.time()), last_success_at=schedule.plan(cfg['main'])['last_success_at'], counts=progress.get('counts', {}))), 0o644)
                                    progress = {}
                                schedule.finish(cfg['main'], False, blocked=progress.get('state') == 'blocked', request_id=nonce)
                                output('preparing', '更新未完成，请先完成组织登录并填写出口 ID；设备分流仍关闭')
                            last_nonce = nonce
                    else:
                        preflight(cfg)
                        needed = {'bypass_cn': 'china.txt', 'gfw': 'gfw.txt', 'whitelist': 'whitelist.txt'}
                        policies = ([{'enabled': '1', 'mode': cfg['main'].get('lan_mode', 'bypass_cn')}] if cfg['main'].get('scope') == 'lan' else cfg['policies'])
                        with rule_snapshot() as snapshot:
                            from rules_overrides import load as load_personal
                            cfg['rule_overrides'] = load_personal(RULES)
                            missing_rules = any(p.get('enabled') == '1' and p.get('mode') in needed and not (snapshot / needed[p['mode']]).exists() and not cfg['rule_overrides'][needed[p['mode']].removesuffix('.txt')]['added'] for p in policies)
                        if missing_rules:
                            vnet = '' if service.backend == 'worker' else resolve_exit(cfg['main'])
                            service.plane.connect(vnet)
                            update_rules()
                        # Existing valid local rules permit startup even if an update source fails.
                        with rule_snapshot() as snapshot:
                            cfg['rule_overrides'] = load_personal(RULES)
                            digest = routing_digest(cfg, snapshot)
                            compiled = compile_config(cfg, snapshot) if digest != previous else None
                            if compiled is not None:
                                from rules_catalog import directory_revision
                                from rules_overrides import effective_revision
                                compiled['rules_revision'] = effective_revision(directory_revision(snapshot), cfg['rule_overrides'])
                        if compiled is not None:
                            compiled['dns']['generation'] = digest
                            service.apply(cfg, compiled)
                            previous = digest
                        service.health()
                        nonce = cfg['main'].get('update_nonce', '')
                        try:
                            if service.update_job and service.update_job.poll() is not None:
                                succeeded = service.update_job.returncode == 0
                                finish_interrupted_update()
                                progress = __import__('rules_schedule').read(RUN / 'update.json')
                                blocked = progress.get('state') == 'blocked'
                                completed = schedule.finish(cfg['main'], succeeded, blocked=blocked, request_id=update_job_id or schedule.data.get('request_id'))
                                update_message = '规则已保存，等待运行版本确认' if succeeded else '规则异常，等待确认' if blocked else '更新失败，保留现有规则并按计划重试'
                                if completed is False:
                                    update_message = '规则状态已由后续确认或回退操作更新'
                                service.update_job = None
                                update_job_id = None
                            if not service.update_job and schedule.due(cfg['main']):
                                job = nonce if nonce and nonce != schedule.data.get('last_nonce') else 'auto-' + uuid.uuid4().hex
                                schedule.start(cfg['main'], job)
                                update_job_id = job
                                try:
                                    service.update_job = subprocess.Popen([sys.executable, __file__, 'update-worker'],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True, env={**os.environ, 'ROUTEPILOT_UPDATE_ID': job})
                                except Exception:
                                    schedule.finish(cfg['main'], False)
                                    update_message = '更新任务未启动，保留现有规则并按计划重试'
                                else:
                                    update_message = '规则正在后台更新'
                                    last_nonce = nonce
                        except OSError:
                            update_message = '更新计划无法持久保存，保留当前线路；请检查存储空间'
                        scope_message = ('所选局域网 ' + '、'.join(v['interface'] + ' ' + v['network'] for v in cfg['main']['lan_networks'])) if service.compiled.get('scope') == 'lan' else '仅接管所选设备'
                        output('running', ('备用线路运行中（TCP，UDP不代理）；' if service.backend == 'worker' else (service.plane.protocol or 'WARP')+' 已连接；') + scope_message + '。' + update_message,
                               service.compiled['devices'], exit=service.compiled['exit'], exit_ip=service.exit_ip,
                               runtime_id=service.runtime_id, generation=digest, protocol='Workers' if service.backend == 'worker' else (service.plane.protocol or 'WARP'), endpoint_generation=__import__('endpoint_control').generation(), protocol_warning=getattr(service.plane,'protocol_warning',''), protocol_observed_at=getattr(service.plane,'protocol_observed_at',0), backend=service.backend, proxy_interface='rp-backup' if service.backend == 'worker' else 'rp-host', backup_name=service.plane.node['name'] if service.backend == 'worker' and service.plane.node else None, scope=service.compiled.get('scope', 'devices'), rules_updated=int(schedule.data.get('last_success_at', 0)), rules_revision=service.compiled.get('rules_revision', ''), applied_nonce=cfg['main'].get('apply_nonce', ''))
            except TunnelPending:
                try:direct=service.suspend_capture()
                except Exception:direct=service.stop(restore=False)['direct']
                previous=None
                output('error','WARP 暂时中断；'+('当前保持宽带直连，正在等待原客户端恢复' if direct else '接管清理未确认，网络路径未知'),fallback='direct' if direct else 'unknown',recovering=True,scope=cfg['main'].get('scope','devices'),attempted_nonce=cfg['main'].get('apply_nonce',''))
            except Exception as error:
                if 'cfg' in locals() and cfg['main'].get('enabled') == '1' and cfg['main'].get('backend') == 'worker':
                    from worker_backend import mark_failed
                    service.worker_failed_nonce = cfg['main'].get('apply_nonce', '')
                    try:
                        mark_failed(cfg['main'].get('apply_nonce', ''))
                    except OSError:
                        pass
                stopped = service.stop(restore=False)
                previous = None
                # Known validation messages contain no credential values.
                message = str(error) if isinstance(error, (ValueError, RuntimeError)) else '服务尚未就绪，请检查依赖'
                current_cfg = cfg if 'cfg' in locals() else {'main': {}}
                scope = current_cfg.get('main', {}).get('scope', 'devices')
                target = '所选局域网' if scope == 'lan' else '所选设备'
                fallback_message = '已撤回当前线路接管，当前允许宽带直连（其他网络策略仍可能影响实际路径）' if stopped['direct'] else '接管清理未能确认，当前网络路径未知，请检查服务'
                output('error', message + '；' + target + fallback_message,
                       fallback='direct' if stopped['direct'] else 'unknown', scope=scope, attempted_nonce=current_cfg.get('main', {}).get('apply_nonce', ''))
            time.sleep(3)
    finally:
        stopped = service.stop(restore=STOP)
        output('stopped', '插件已停止；' + stopped['legacy'] + ('；接管已撤回' if stopped['direct'] else '；清理未能确认，网络路径未知'), fallback='unknown')
        lock.close()


if __name__ == '__main__':
    main()
