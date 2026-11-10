"""WARP maintenance: metadata checks never install; writes require a bound plan.

Only the owned Debian 12 ARM64 runtime is managed. All cloud identities remain
private. No route, DNS, UCI, registration, or Cloudflare API is changed here.
"""
from contextlib import contextmanager
import fcntl
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid

from runtime import RUN, atomic, read_config
from request_input import claim_request, prepare_requests
from public_errors import ControlError
import runtime_bootstrap as bootstrap

BASE = Path('/opt/routepilot')
PRIVATE = Path('/etc/routepilot/warp-maintenance')
SOURCE = 'https://pkg.cloudflareclient.com/'
KEY_SHA256 = '0f37fc298c98e88ee3c0ee68c95b69f1dba9eb477abe3167e13982105911264d'
MAX_UPLOAD = 256 * 1024 * 1024
MAX_UNPACKED = 1536 * 1024 * 1024
OMIT = {'proc', 'sys', 'dev', 'run', 'tmp'}
VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){2,5}(?:-[0-9]+)?')


def directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid():
        raise ControlError('维护目录类型或属主异常，未继续操作')
    path.chmod(0o700)


def document(path, default=None):
    if not path.exists() and not path.is_symlink():
        return default
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077 or s.st_size > 65536:
                raise ValueError()
            value = json.load(f)
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except (OSError, ValueError):
        raise ControlError('维护记录损坏或权限异常，请由管理员检查；未重置记录') from None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def save(path, value):
    directory(path.parent)
    atomic(path, json.dumps(value, ensure_ascii=False) + '\n')
    sync_directory(path.parent)


def prepare():
    directory(BASE)
    directory(PRIVATE)
    directory(RUN)
    prepare_requests(RUN)
    directory(BASE / 'runtime-uploads')
    return {'ready': True}


@contextmanager
def job_lock():
    directory(PRIVATE)
    fd = os.open(PRIVATE / '.job.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError('已有版本检查或维护任务正在执行，请稍后再试') from None
        yield
    finally:
        os.close(fd)


def fields(text):
    return [dict(line.split(': ', 1) for line in p.splitlines() if ': ' in line and not line.startswith(' ')) for p in text.split('\n\n')]


def package_info(root, probe=True):
    # Older imOK runtimes unpacked the official .deb directly. Their binaries
    # work, but cloudflare-warp is absent from dpkg's database. Do not call that
    # a missing installation, and never guess a version from a filename.
    try:
        if root.is_symlink() or not all((root / x).is_file() for x in ('usr/bin/warp-cli', 'usr/bin/warp-svc', 'usr/bin/apt-get')):
            return None
        p = root / 'var/lib/dpkg/status'
        if p.stat().st_size > 16 * 1024 * 1024:return None
        rows = [r for r in fields(p.read_text()) if r.get('Package') == 'cloudflare-warp']
        if rows:
            r = rows[0]
            if len(rows) != 1 or r.get('Status') != 'install ok installed' or not VERSION.fullmatch(r.get('Version', '')) or r.get('Architecture') != 'arm64':return None
            return {'version': r['Version'], 'architecture': 'arm64', 'package_managed': True}
        with (root / 'usr/bin/warp-cli').open('rb') as f:header = f.read(20)
        if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:return None
        version = None
        if probe:
            text = run(['chroot', str(root), '/usr/bin/warp-cli', '--version'], 5, isolated=True)
            match = re.search(r'warp-cli ([0-9]+(?:\.[0-9]+){2,5}(?:-[0-9]+)?)', text)
            if not match:return None
            version = match[1]
        return {'version': version, 'architecture': 'arm64', 'package_managed': False}
    except (OSError, ValueError, IndexError, ControlError):
        return None


def version_key(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ControlError('官方版本格式暂不支持，未提供自动安装操作')
    return tuple(int(x) for x in re.split('[.-]', value))


def supported():
    try:
        return os.uname().machine == 'aarch64' and re.search(r"DISTRIB_RELEASE=['\"]?24\.10", Path('/etc/openwrt_release').read_text()) is not None
    except OSError:
        return False


def offline_network():
    # Used only by a dedicated maintenance child. Never alter the router or
    # the supervisor namespace. Refuse installation if isolation is unavailable.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x40000000) != 0:  # CLONE_NEWNET
        os._exit(126)


def run(args, timeout=30, isolated=False):
    child = None
    try:
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                 preexec_fn=offline_network if isolated else None,
                                 env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'SYSTEMD_OFFLINE': '1', 'LC_ALL': 'C'})
        out, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if child is not None:
            try:os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:pass
            child.communicate()
        raise ControlError('维护命令超时，已终止本次维护命令组；未提交现用环境') from None
    except (OSError, subprocess.SubprocessError):
        raise ControlError('维护命令或隔离环境未能启动，未提交升级') from None
    if child.returncode:
        raise ControlError('安装或校验命令失败，现用环境未提交；请检查官方源连通性、依赖、隔离权限与存储空间')
    return out.decode(errors='replace')


def download(relative, destination, limit):
    if not re.fullmatch(r'(pubkey\.gpg|dists/bookworm/main/binary-arm64/Packages)', relative):
        raise ControlError('软件源地址无效')
    run(['curl', '--fail', '--silent', '--show-error', '--proto', '=https', '--max-time', '45',
         '--max-filesize', str(limit), '--output', str(destination), SOURCE + relative], 50)
    if not destination.is_file() or destination.stat().st_size > limit:
        raise ControlError('官方返回内容大小异常，未使用该结果')
    return destination.read_bytes()


def candidate(text):
    rows = []
    for r in fields(text):
        if r.get('Package') != 'cloudflare-warp' or r.get('Architecture') != 'arm64':
            continue
        v = r.get('Version', '')
        if not VERSION.fullmatch(v) or not re.fullmatch('[a-f0-9]{64}', r.get('SHA256', '')):
            raise ControlError('官方软件目录字段异常，未标记为可更新')
        expected = 'pool/bookworm/main/c/cloudflare-warp/cloudflare-warp_' + v + '_arm64.deb'
        if r.get('Filename') != expected or not r.get('Size', '').isdigit() or not 1024 <= int(r['Size']) <= MAX_UPLOAD:
            raise ControlError('官方软件包路径或大小异常')
        rows.append({'version': v, 'sha256': r['SHA256'], 'size': int(r['Size'])})
    if not rows:
        raise ControlError('官方源没有可识别的 Debian 12 ARM64 WARP 软件包')
    return max(rows, key=lambda r: version_key(r['version']))


def check():
    with job_lock():
        previous = document(PRIVATE / 'check.json', {})
        state = {**previous, 'last_attempt_at': time.time(), 'state': 'checking', 'message': '正在读取 Cloudflare 官方稳定源'}
        save(PRIVATE / 'check.json', state)
        cache = PRIVATE / 'Packages.download'
        try:
            value = candidate(download('dists/bookworm/main/binary-arm64/Packages', cache, 8 * 1024 * 1024).decode())
            state.update(state='checked', candidate=value, checked_at=time.time(), message='官方版本目录已读取；实际升级仍需验证软件源签名及软件包校验值')
        except Exception as e:
            state.update(state='failed', message=str(e) if isinstance(e, ControlError) else '版本检查失败；未将历史结果当作最新结果')
        finally:
            cache.unlink(missing_ok=True)
            save(PRIVATE / 'check.json', state)
    return state


def settings():
    d = document(PRIVATE / 'settings.json', {'hours': 24})
    if set(d) != {'hours'} or type(d['hours']) is not int or d['hours'] not in (0, 24, 72, 168):
        raise ControlError('检查频率记录异常，未恢复为默认设置')
    return d


def due(now=None):
    now = time.time() if now is None else now
    hours = settings()['hours']
    c = document(PRIVATE / 'check.json', {})
    last = c.get('last_attempt_at', 0)
    return bool(hours and (not last or now < last - 300 or now - last >= hours * 3600))


def identity(root):
    p = root / 'var/lib/cloudflare-warp'
    h = hashlib.sha256((fingerprint(p, False) if p.exists() else 'missing').encode())
    for rel in ('etc/machine-id', 'var/lib/dbus/machine-id'):
        f = root / rel
        h.update(rel.encode())
        if f.is_symlink():h.update(os.readlink(f).encode())
        elif f.is_file():h.update(f.read_bytes())
    return h.hexdigest()


def fingerprint(root, exclude_mounts=True):
    if root.is_symlink():
        raise ControlError('运行环境或身份根目录为符号链接，未继续操作')
    h = hashlib.sha256()
    if not root.exists():
        return 'missing'
    for folder, dirs, files in os.walk(root, followlinks=False):
        rel = Path(folder).relative_to(root)
        if exclude_mounts and rel == Path('.'):
            dirs[:] = [d for d in dirs if d not in OMIT]
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(folder) / name
            s = p.lstat()
            h.update(json.dumps([str(p.relative_to(root)), stat.S_IFMT(s.st_mode), s.st_mode & 0o7777, s.st_size if stat.S_ISREG(s.st_mode) else 0], separators=(',', ':')).encode() + b'\0')
            if stat.S_ISLNK(s.st_mode):
                h.update(b'link:' + os.readlink(p).encode())
            elif stat.S_ISREG(s.st_mode):
                with p.open('rb') as f:
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        h.update(block)
            elif not stat.S_ISDIR(s.st_mode):
                raise ControlError('运行环境含不支持的特殊文件，未制作恢复副本')
    return h.hexdigest()


def context():
    p = BASE / 'runtime'
    # Full identities/configs are never returned to RPC.
    cfg = read_config()['main']
    try:boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:boot = 'unavailable'
    return hashlib.sha256(boot.encode() + json.dumps(cfg, sort_keys=True).encode() + identity(p).encode() + json.dumps(package_info(p), sort_keys=True).encode() + str(p.stat().st_ino if p.exists() else 0).encode()).hexdigest()


def inactive():
    cfg = read_config()['main']
    if cfg.get('enabled', '0') != '0' or cfg.get('preparing', '0') != '0':
        raise ControlError('请先在总览关闭 imOK，并结束登录准备，再进行安装、升级或回滚；版本检查不受影响')
    if (RUN / 'masque-owner.json').exists():
        raise ControlError('WARP 运行环境仍被占用，请等待关闭完成后刷新')
    root = str(BASE / 'runtime')
    try:
        for p in Path('/proc').glob('[0-9]*/root'):
            try:
                if os.readlink(p).rstrip('/') == root:
                    raise ControlError('WARP 环境还有进程运行，未替换文件')
            except (FileNotFoundError, PermissionError):
                pass
        mounts = Path('/proc/self/mountinfo').read_text()
        if any(line.split()[4].replace('\\040', ' ').startswith(root + '/') for line in mounts.splitlines()):
            raise ControlError('WARP 环境仍有挂载，请由管理员检查，未自动卸载')
    except FileNotFoundError:
        raise ControlError('无法确认 Linux 进程和挂载状态，未执行维护') from None


def estimate(root):
    total = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        if Path(folder) == root:
            dirs[:] = [d for d in dirs if d not in OMIT]
        for name in files:
            p = Path(folder) / name
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def backups():
    result = []
    folder = BASE / 'runtime-backups'
    if not folder.exists():
        return result
    if folder.is_symlink():
        raise ControlError('恢复目录类型异常')
    for p in folder.iterdir():
        if not re.fullmatch('[a-f0-9]{32}', p.name) or p.is_symlink() or not p.is_dir():
            continue
        d = document(p / 'meta.json', {})
        if d.get('id') == p.name and (p / 'runtime').is_dir() and not (p / 'runtime').is_symlink():
            result.append({'id': p.name, 'version': d.get('version'), 'created_at': d.get('created_at')})
    return sorted(result, key=lambda r: r['created_at'], reverse=True)


def status():
    c = document(PRIVATE / 'check.json', {})
    s = settings()
    j = document(PRIVATE / 'job.json', {})
    info = package_info(BASE / 'runtime', probe=j.get('state') != 'running')
    if info and info['version'] is None and j.get('state') == 'running':
        info['version'] = j.get('local_version')
    fresh = c.get('state') == 'checked' and -300 <= time.time() - c.get('checked_at', 0) < 7 * 86400
    remote = c.get('candidate') if fresh else None
    if j.get('state') == 'running' and time.time() - j.get('updated_at', 0) > 1800:
        j = {**j, 'state': 'interrupted', 'message': '维护任务未完成，请核对恢复副本；未自动重试安装'}
    return {'schema': 1, 'supported': supported(), 'installed': info is not None, 'runtime_exists': (BASE / 'runtime').exists(),
            'local_version': info['version'] if info else None, 'check': c, 'settings': s,
            'update_available': bool(info and info['version'] and remote and version_key(remote['version']) > version_key(info['version'])),
            'latest_version': remote['version'] if remote else None, 'backups': backups(), 'job': j,
            'recovery_required': (BASE / '.runtime-transaction.json').exists(),
            'next_check_at': c.get('last_attempt_at', 0) + s['hours'] * 3600 if s['hours'] and c.get('last_attempt_at') else 0,
            'automatic_install': False, 'environment': bootstrap.detect(sys.modules[__name__]),
            'online': bootstrap.summary(sys.modules[__name__])}


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def claim_upload(expected, target):
    p = BASE / 'runtime-uploads/runtime.tgz'
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as src, target.open('xb') as dst:
        s = os.fstat(src.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or not 1 <= s.st_size <= MAX_UPLOAD:
            raise ControlError('上传包类型或大小异常')
        os.fchmod(dst.fileno(), 0o600)
        remaining = MAX_UPLOAD + 1
        while remaining:
            block = src.read(min(1024 * 1024, remaining))
            if not block:break
            dst.write(block);remaining -= len(block)
        dst.flush();os.fsync(dst.fileno())
    if not remaining or file_hash(target) != expected:
        raise ControlError('上传包已变化或校验不符，未安装')


def upload_hash():
    p = BASE / 'runtime-uploads/runtime.tgz'
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or not 1 <= s.st_size <= MAX_UPLOAD:
            raise ControlError('上传包类型、属主或大小不正确')
        os.fchmod(f.fileno(), 0o600)
        h = hashlib.sha256()
        remaining = MAX_UPLOAD + 1
        while remaining:
            block = f.read(min(1024 * 1024, remaining))
            if not block:break
            h.update(block);remaining -= len(block)
        if not remaining:raise ControlError('上传文件超过大小限制')
        return h.hexdigest(), s.st_size


def make_plan(payload):
    if not supported():
        raise ControlError('目前只支持 ARM64 OpenWrt 24.10，其他环境不执行安装')
    inactive()
    action = payload.get('operation')
    if action not in ('install', 'online-install', 'upgrade', 'rollback', 'recover'):
        raise ControlError('维护操作无效')
    info = package_info(BASE / 'runtime')
    if (BASE / '.runtime-transaction.json').exists() and action not in ('rollback', 'recover'):
        raise ControlError('上次替换未完成，请先回滚恢复')
    p = {'id': uuid.uuid4().hex, 'operation': action, 'created_at': time.time(), 'context': context(), 'old_version': info['version'] if info else None}
    need = 512 * 1024 * 1024
    if action == 'online-install':
        if (BASE / 'runtime').exists() or (BASE / 'runtime').is_symlink():
            raise ControlError('已检测到运行环境，不会覆盖安装；请使用更新')
        p.update(bootstrap.plan(sys.modules[__name__]))
        need = p['required_bytes']
    elif action == 'install':
        if (BASE / 'runtime').exists() or (BASE / 'runtime').is_symlink():
            raise ControlError('已有运行环境，首次安装不能覆盖；请使用升级或人工修复')
        expected = payload.get('sha256', '')
        if not re.fullmatch('[a-f0-9]{64}', expected):
            raise ControlError('请填写可信发布者提供的 SHA-256，不能只凭文件名安装')
        actual, size = upload_hash()
        if actual != expected:
            raise ControlError('运行环境包校验不符，未安装')
        p.update(sha256=actual, upload_size=size)
        need = MAX_UNPACKED + 64 * 1024 * 1024
    elif action == 'upgrade':
        s = status()
        if not info or not s['update_available']:
            raise ControlError('没有有效的新版本信息，请先检查更新；不会降级或重复安装')
        try:base_release = (BASE / 'runtime/usr/lib/os-release').read_text()
        except OSError:base_release = ''
        if 'VERSION_CODENAME=bookworm' not in base_release:
            raise ControlError('已有运行环境不是受支持的 Debian 12 bookworm，未提供自动升级')
        p['candidate'] = s['check']['candidate']
        p['version'] = s['latest_version']
        need = estimate(BASE / 'runtime') * 2 + 256 * 1024 * 1024
    elif action == 'recover':
        journal = document(BASE / '.runtime-transaction.json')
        if not journal or journal.get('phase') not in ('prepared', 'verified'):
            raise ControlError('环境已进入替换阶段，请选择恢复副本回滚；不能直接解除阻断')
        p['transaction_id'] = journal.get('id')
        need = 0
    else:
        found = next((b for b in backups() if b['id'] == payload.get('backup_id')), None)
        if not found:
            raise ControlError('所选恢复副本不可用')
        p.update(backup_id=found['id'], version=found['version'])
        need = estimate(BASE / 'runtime-backups' / found['id'] / 'runtime') + 256 * 1024 * 1024
    free = shutil.disk_usage(BASE).free
    if free < need:
        raise ControlError('可用存储不足，需要至少 %.0f MB；请先释放空间，未删除任何恢复副本' % (need / 1048576))
    p['required_bytes'] = need
    save(PRIVATE / 'plan.json', p)
    return {k: v for k, v in p.items() if k not in ('context', 'candidate', 'sha256')}


def unpack(source, target):
    """Never follow archive links while writing. No devices, escapes or credentials."""
    with tarfile.open(source, 'r:gz') as tf:
        members, total = [], 0
        for m in tf:
            members.append(m)
            total += m.size
            if len(members) > 100000 or total > MAX_UNPACKED or m.size < 0:
                raise ControlError('运行环境包解压大小或文件数量超限')
        seen, links = set(), []
        for m in members:
            q = PurePosixPath(m.name)
            if q.is_absolute() or '..' in q.parts:
                raise ControlError('运行环境包含越界路径')
            name = str(q)
            if name == '.':
                continue
            if name in OMIT | {'etc', 'usr', 'usr/bin', 'usr/sbin', 'usr/lib', 'usr/share', 'var', 'var/lib', 'var/lib/cloudflare-warp'} and not m.isdir():
                raise ControlError('运行环境的关键目录不能是文件或链接')
            if name in seen:
                raise ControlError('运行环境包含重复文件名')
            seen.add(name)
            if (name.startswith('var/lib/cloudflare-warp/') and not m.isdir()) or name in ('etc/machine-id', 'var/lib/dbus/machine-id') and m.size:
                raise ControlError('运行环境包包含已注册身份或设备标识，首次安装只接受未注册包')
            if '.ssh' in q.parts or 'reg.json' in q.parts or any(x in q.parts for x in ('id_rsa', 'id_ed25519')):
                raise ControlError('运行环境包包含私人凭据目录')
            if not (m.isdir() or m.isfile() or m.issym() or m.islnk()):
                raise ControlError('运行环境包包含特殊设备或高权限文件')
            if m.issym() or m.islnk():
                links.append(m)
        for m in members:
            q = PurePosixPath(m.name)
            if str(q) == '.' or m.issym() or m.islnk():
                continue
            p = target.joinpath(*q.parts)
            p.parent.mkdir(parents=True, exist_ok=True)
            if m.isdir():
                p.mkdir(exist_ok=True)
            else:
                with tf.extractfile(m) as src, p.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            p.chmod(m.mode & 0o1777)
        for m in links:
            p = target.joinpath(*PurePosixPath(m.name).parts)
            # Links cannot be a parent of another archive member.
            rel = str(PurePosixPath(m.name))
            if any(n.startswith(rel + '/') for n in seen):
                raise ControlError('运行环境包的链接与目录冲突')
            p.parent.mkdir(parents=True, exist_ok=True)
            if m.issym():
                if not m.linkname or '\x00' in m.linkname:
                    raise ControlError('运行环境包链接无效')
                p.symlink_to(m.linkname)
            else:
                q = PurePosixPath(m.linkname)
                t = target.joinpath(*q.parts)
                if q.is_absolute() or '..' in q.parts or not t.is_file() or t.is_symlink() or not str(t.resolve()).startswith(str(target.resolve()) + os.sep):
                    raise ControlError('运行环境包硬链接无效')
                os.link(t, p)


def copy_runtime(source, target):
    def ignore(folder, names):
        return OMIT & set(names) if Path(folder) == source else set()
    shutil.copytree(source, target, symlinks=True, ignore=ignore)
    for name in OMIT:
        (target / name).mkdir(exist_ok=True)


def internal(root, rel, content, mode=0o644):
    p = root / rel
    # Never let a chroot-absolute symlink write into the host from Python.
    current = p.parent
    while current != root:
        if current.is_symlink():
            raise ControlError('运行环境的维护写入目录包含链接，未继续')
        current = current.parent
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink():
        p.unlink()
    atomic(p, content, mode)


def test_runtime(root, version=None):
    for rel in OMIT | {'etc', 'usr', 'usr/bin', 'usr/lib', 'var', 'var/lib'}:
        p = root / rel
        if p.is_symlink() or not p.is_dir():
            raise ControlError('运行环境关键目录结构异常，未执行程序')
    info = package_info(root)
    try:
        os_release = (root / 'usr/lib/os-release').read_text()
    except OSError:
        raise ControlError('运行环境缺少 Debian 版本信息') from None
    if not info or 'VERSION_CODENAME=bookworm' not in os_release or version and info['version'] != version:
        raise ControlError('运行环境不是受支持的 Debian 12 ARM64 WARP，未替换现用环境')
    text = run(['chroot', str(root), '/usr/bin/warp-cli', '--version'], 15, isolated=True)
    if info['version'] not in text:
        raise ControlError('WARP 程序自检版本不一致，未提交')
    run(['chroot', str(root), '/usr/bin/warp-svc', '--help'], 15, isolated=True)
    return info


def upgrade_copy(root, plan):
    keyfile = PRIVATE / 'key.download'
    try:
        key = download('pubkey.gpg', keyfile, 65536)
        if hashlib.sha256(key).hexdigest() != KEY_SHA256:
            raise ControlError('官方签名公钥发生变化，需先审核公钥；未自动信任新密钥')
        internal(root, 'usr/share/keyrings/imok-cloudflare.asc', key.decode())
    finally:
        keyfile.unlink(missing_ok=True)
    internal(root, 'etc/apt/sources.list.d/imok-warp.list', 'deb [arch=arm64 signed-by=/usr/share/keyrings/imok-cloudflare.asc] ' + SOURCE + ' bookworm main\n' + 'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main\n' + 'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian-security bookworm-security main\n')
    internal(root, 'usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n', 0o755)
    internal(root, 'etc/resolv.conf', Path('/etc/resolv.conf').read_text())
    for name, minor in [('null', 3), ('zero', 5), ('random', 8), ('urandom', 9)]:
        os.mknod(root / 'dev' / name, stat.S_IFCHR | 0o666, os.makedev(1, minor))
    args = ['chroot', str(root), '/usr/bin/apt-get', '-o', 'Dir::Etc::sourcelist=sources.list.d/imok-warp.list',
            '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::State::lists=lists-imok', '-o', 'Acquire::Retries=1',
            '-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30']
    (root / 'var/lib/apt/lists-imok/partial').mkdir(parents=True, exist_ok=True)
    run(args + ['update'], 180)
    # APT checks the signed InRelease; independently bind the downloaded .deb to
    # the version/size/SHA the user confirmed. Never execute an unverified .deb.
    cache = root / 'var/cache/apt/archives'
    for p in cache.glob('cloudflare-warp_*.deb'):
        p.unlink()
    install = ['--yes', '--no-remove', '--no-install-recommends', 'install', 'cloudflare-warp=' + plan['version']]
    run(args + ['--download-only'] + install, 600)
    files = list(cache.glob('cloudflare-warp_*.deb'))
    wanted = plan['candidate']
    if len(files) != 1 or files[0].stat().st_size != wanted['size'] or file_hash(files[0]) != wanted['sha256']:
        raise ControlError('软件下载结果与确认版本的校验值不一致，未安装')
    run(args + ['--no-download'] + install, 600, isolated=True)


def progress(message, state='running'):
    previous = document(PRIVATE / 'job.json', {})
    save(PRIVATE / 'job.json', {**previous, 'state': state, 'message': message, 'updated_at': time.time()})


def swap(stage, version):
    root = BASE / 'runtime'
    ident = uuid.uuid4().hex
    backup = BASE / 'runtime-backups' / ident
    directory(backup)
    journal = BASE / '.runtime-transaction.json'
    old = package_info(root)
    before = fingerprint(root) if root.exists() else None
    save(backup / 'meta.json', {'id': ident, 'version': old['version'] if old else None, 'created_at': time.time(), 'fingerprint': before})
    save(journal, {'id': ident, 'phase': 'prepared', 'had_runtime': root.exists(), 'new_version': version, 'new_fingerprint': fingerprint(stage)})
    try:
        if root.exists():
            os.rename(root, backup / 'runtime')
            sync_directory(backup);sync_directory(BASE)
        save(journal, {'id': ident, 'phase': 'old_moved', 'had_runtime': before is not None})
        os.rename(stage, root)
        sync_directory(BASE)
        # No daemon or network service is started by a successful transaction.
        test_runtime(root, version)
        save(journal, {'id': ident, 'phase': 'verified', 'had_runtime': before is not None, 'new_version': version, 'new_fingerprint': fingerprint(root)})
        journal.unlink();sync_directory(BASE)
    except Exception:
        if (backup / 'runtime').exists():
            if root.exists():
                os.rename(root, BASE / ('.runtime-failed-' + ident))
            os.rename(backup / 'runtime', root)
            sync_directory(BASE);sync_directory(backup)
            journal.unlink(missing_ok=True);sync_directory(BASE)
        elif before is None and root.exists():
            os.rename(root, BASE / ('.runtime-failed-' + ident))
            journal.unlink(missing_ok=True);sync_directory(BASE)
        elif before is not None and root.exists() and fingerprint(root) == before:
            journal.unlink(missing_ok=True);sync_directory(BASE)
        raise


def recover(plan):
    path = BASE / '.runtime-transaction.json'
    d = document(path)
    if not d or d.get('id') != plan.get('transaction_id') or not re.fullmatch('[a-f0-9]{32}', d.get('id', '')):
        raise ControlError('维护记录已变化，请重新核对')
    source = BASE / 'runtime-backups' / d['id']
    meta = document(source / 'meta.json')
    if not meta:raise ControlError('维护证据缺失，未解除阻断')
    root = BASE / 'runtime'
    if d['phase'] == 'prepared' and not (source / 'runtime').exists():
        if meta.get('fingerprint') is None:
            if root.exists():raise ControlError('原空安装状态已变化，未解除阻断')
        elif fingerprint(root) != meta.get('fingerprint'):
            raise ControlError('现用环境与维护前不一致，请使用恢复副本')
    elif d['phase'] == 'verified' and fingerprint(root) == d.get('new_fingerprint'):
        test_runtime(root, d.get('new_version'))
    else:
        raise ControlError('不能证明文件替换已安全结束，请选择恢复副本回滚')
    path.unlink();sync_directory(BASE)
    progress('未完成维护已核对，现用文件保持；仍需手动开启并检查联网', 'success')


def execute(plan_id, confirmation):
    if confirmation != 'CONFIRM' or not re.fullmatch('[a-f0-9]{32}', plan_id or ''):
        raise ControlError('缺少明确的维护确认，未执行')
    with job_lock():
        p = document(PRIVATE / 'plan.json', {})
        if p.get('id') != plan_id or not -5 <= time.time() - p.get('created_at', 0) <= 300:
            raise ControlError('维护确认已过期或已使用，请重新检查')
        if not supported() or p.get('context') != context():
            raise ControlError('设备、账户或配置已变化，请重新检查；未执行旧确认')
        directory(RUN)
        from runtime_guard import maintenance
        with maintenance(RUN, BASE):
            inactive()
            (PRIVATE / 'plan.json').unlink()
            if shutil.disk_usage(BASE).free < p['required_bytes']:
                raise ControlError('可用存储已变化，未开始维护')
            if p['operation'] == 'recover':
                recover(p)
                return
            stage = BASE / ('.runtime-stage-' + uuid.uuid4().hex)
            upload_copy = PRIVATE / ('claimed-' + uuid.uuid4().hex + '.tgz')
            try:
                progress('正在准备独立副本；原环境尚未替换')
                if p['operation'] == 'install':
                    stage.mkdir(mode=0o700)
                    claim_upload(p['sha256'], upload_copy)
                    unpack(upload_copy, stage)
                    test_runtime(stage)
                    registration = stage / 'var/lib/cloudflare-warp'
                    if registration.is_symlink() or registration.exists() and any(registration.iterdir()):
                        raise ControlError('首次安装包含身份数据，未安装')
                elif p['operation'] == 'online-install':
                    bootstrap.install(sys.modules[__name__], p, stage)
                elif p['operation'] == 'upgrade':
                    copy_runtime(BASE / 'runtime', stage)
                    original_identity = identity(BASE / 'runtime')
                    progress('正在副本中下载并验证官方签名；不会启动隧道')
                    upgrade_copy(stage, p)
                    test_runtime(stage, p['version'])
                    if identity(stage) != original_identity:
                        raise ControlError('升级程序改动了账户或配置，未提交该副本')
                else:
                    source = BASE / 'runtime-backups' / p['backup_id']
                    meta = document(source / 'meta.json')
                    if not meta or fingerprint(source / 'runtime') != meta.get('fingerprint'):
                        raise ControlError('恢复副本校验失败，未回滚')
                    copy_runtime(source / 'runtime', stage)
                    current = BASE / 'runtime/var/lib/cloudflare-warp'
                    if current.exists():
                        target = stage / 'var/lib/cloudflare-warp'
                        if target.is_symlink():
                            raise ControlError('恢复副本身份目录异常')
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(current, target, symlinks=True)
                    for rel in ('etc/machine-id', 'var/lib/dbus/machine-id'):
                        f = BASE / 'runtime' / rel
                        if f.is_file() and not f.is_symlink():internal(stage, rel, f.read_text())
                        elif f.is_symlink():
                            dest = stage / rel
                            internal(stage, rel, '')
                            dest.unlink();dest.symlink_to(os.readlink(f))
                    if (BASE / 'runtime').exists() and identity(stage) != identity(BASE / 'runtime'):
                        raise ControlError('恢复副本未能保留当前身份，未回滚')
                    test_runtime(stage, p['version'])
                inactive()
                if p['context'] != context():
                    raise ControlError('维护期间账户或配置已变化，未提交新环境；请重新检查后确认')
                progress('副本检查通过；正在保留原环境并提交替换')
                swap(stage, package_info(stage)['version'])
                if p['operation'] == 'online-install':
                    save(PRIVATE / 'online.json', {'state': 'installed', 'version': p['version'], 'message': 'WARP 已安装，请登录自己的账户并手动开启'})
                progress('WARP 环境已安装' if p['operation'] in ('install', 'online-install') else 'WARP 已升级，原环境已保留用于回滚' if p['operation'] == 'upgrade' else 'WARP 已回滚，当前账户配置已保留', 'success')
            except Exception as e:
                if p['operation'] == 'online-install':
                    save(PRIVATE / 'online.json', {'state': 'failed', 'message': str(e) if isinstance(e, ControlError) else '安装未完成，请重新准备；未自动启用'})
                progress(str(e) if isinstance(e, ControlError) else '维护未完成，请检查恢复状态；未自动启动或重试', 'failed')
                raise
            finally:
                upload_copy.unlink(missing_ok=True)
                if stage.exists() and not stage.is_symlink():
                    shutil.rmtree(stage)


def launch(action, token):
    subprocess.Popen(['/usr/bin/python3', '/usr/libexec/routepilot-runtime', '_job', action, token],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def dispatch(action, request_id):
    prepare()
    with claim_request(RUN / 'runtime-input', request_id, 4096) as payload:
        if not isinstance(payload, dict):
            raise ControlError('维护请求格式无效')
        if action == 'settings':
            h = payload.get('hours')
            if type(h) is not int or h not in (0, 24, 72, 168):
                raise ControlError('请选择支持的检查频率')
            save(PRIVATE / 'settings.json', {'hours': h})
            result = {'saved': True}
        elif action == 'plan':
            with job_lock():
                j = document(PRIVATE / 'job.json', {})
                if j.get('state') == 'running' and time.time() - j.get('updated_at', 0) < 1800:
                    raise ControlError('已有维护任务正在执行，请等待结果')
                result = {'plan': make_plan(payload)}
        elif action in ('check', 'execute', 'prepare-online'):
            with job_lock():
                j = document(PRIVATE / 'job.json', {})
                if j.get('state') == 'running' and time.time() - j.get('updated_at', 0) < 1800:
                    raise ControlError('已有维护任务正在执行，请等待结果')
                token = uuid.uuid4().hex
                save(PRIVATE / 'pending.json', {'action': action, 'token': token, 'payload': payload, 'created_at': time.time()})
                local = package_info(BASE / 'runtime')
                save(PRIVATE / 'job.json', {'state': 'queued', 'local_version': local['version'] if local else None})
                progress('正在检查版本' if action == 'check' else '正在自动检测并准备安装文件' if action == 'prepare-online' else '维护请求已提交；正在再次核对条件')
            try:
                launch(action, token)
            except Exception:
                progress('维护进程未能启动，请重试；未执行安装', 'failed')
                raise ControlError('维护进程未能启动') from None
            result = {'submitted': True}
        else:
            raise ControlError('不支持的维护操作')
    return {'request_id': request_id.split(':')[0], **result}


def job(action, token):
    # Claim a queued operation once; a scheduler cannot consume a UI confirmation.
    with job_lock():
        d = document(PRIVATE / 'pending.json', {})
        if d.get('token') != token or d.get('action') != action or not -5 <= time.time() - d.get('created_at', 0) < 60:
            raise ControlError('维护队列已过期')
        (PRIVATE / 'pending.json').unlink()
    try:
        if action == 'check':
            c = check()
            progress(c['message'], 'success' if c['state'] == 'checked' else 'failed')
        elif action == 'prepare-online':
            bootstrap.prepare(sys.modules[__name__])
        elif action == 'execute':
            p = d['payload']
            execute(p.get('plan_id'), p.get('confirmation'))
    except Exception as e:
        progress(str(e) if isinstance(e, ControlError) else '维护任务未完成，请刷新核对恢复状态', 'failed')


def watch():
    # One process, bounded daily metadata reads; independent of tunnel enablement.
    while True:
        try:
            j = document(PRIVATE / 'job.json', {})
            if j.get('state') == 'running' and time.time() - j.get('updated_at', 0) > 120:
                with job_lock():
                    progress('上次维护进程未完成，请核对恢复状态；未自动重试安装', 'interrupted')
                j = document(PRIVATE / 'job.json', {})
            if j.get('state') != 'running' and due():
                check()
        except Exception:
            pass
        time.sleep(60)
