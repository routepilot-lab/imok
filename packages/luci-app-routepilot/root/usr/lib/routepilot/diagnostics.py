"""Bounded HTTPS reachability checks. No configurable shell/URL execution."""
import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import time

from runtime import RUN, atomic, command
from mplane import net

TARGETS = {
    'google': ('Google', 'https://www.google.com/', 'proxy'),
    'youtube': ('YouTube', 'https://www.youtube.com/', 'proxy'),
    'github': ('GitHub', 'https://github.com/', 'proxy'),
    'taobao': ('淘宝', 'https://taobao.com/', 'direct'),
    'baidu': ('百度', 'https://www.baidu.com/', 'direct'),
}


def classify(code, output):
    parts = output.strip().split()
    http = int(parts[0]) if parts and parts[0].isdigit() else 0
    try:
        ms = round(float(parts[1]) * 1000)
    except (ValueError, IndexError):
        ms = None
    if code == 0 and 200 <= http < 400:
        return dict(state='ok', message='可连接', http=http, ms=ms)
    if code == 0 and http >= 400:
        return dict(state='restricted', message='可达，但返回 HTTP ' + str(http), http=http, ms=ms)
    messages = {6: 'DNS 解析失败', 7: '连接失败', 18: '响应传输未完成', 28: '超时', 35: 'TLS 握手失败', 47: '重定向次数超限', 60: '证书验证失败'}
    return dict(state='failed', message=messages.get(code, '未收到 HTTPS 响应'), http=http, ms=ms)


def probe(key):
    name, url, route = TARGETS[key]
    args = ['curl', '-4', '-I', '-sS', '-L', '--max-redirs', '3', '--connect-timeout', '4',
            '--max-time', '10', '-o', '/dev/null', '-w', '%{http_code} %{time_total}', url]
    try:
        executor = net if route == 'proxy' else command
        if route == 'proxy':
            st = json.loads((RUN / 'status.json').read_text())
            if st.get('backend') == 'worker' and st.get('state')=='running' and -5<=time.time()-st.get('time',0)<=15:
                from worker_backend import curl as executor
        result = executor(args, check=False)
        result = classify(result.returncode, result.stdout)
    except Exception:
        result = dict(state='failed', message='检测未完成', http=0, ms=None)
    return dict(id=key, name=name, route=route, **result)


def worker(key='all'):
    if key != 'all' and key not in TARGETS:
        raise ValueError('Unknown target')
    lock = open(RUN / 'connectivity.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    keys = list(TARGETS) if key == 'all' else [key]
    try:
        current = json.loads((RUN / 'status.json').read_text())
    except Exception:
        current = {}
    items = {k: dict(id=k, name=TARGETS[k][0], route=TARGETS[k][2], state='pending', message='等待检测') for k in keys}
    state = dict(state='running', started_at=int(time.time()), finished_at=0, exit=current.get('exit'),
                 exit_ip=current.get('exit_ip'), runtime_id=current.get('runtime_id'), generation=current.get('generation'), applied_nonce=current.get('applied_nonce'), results=list(items.values()), stale=False)
    def publish():
        state['results'] = list(items.values())
        atomic(RUN / 'connectivity.json', json.dumps(state, ensure_ascii=False), 0o644)
    publish()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(probe, k): k for k in keys}
        for future in concurrent.futures.as_completed(futures):
            items[futures[future]] = future.result()
            publish()
    try:
        now = json.loads((RUN / 'status.json').read_text())
        state['stale'] = now.get('state') != 'running' or any(now.get(k) != state[k] for k in ('exit', 'exit_ip', 'generation', 'applied_nonce', 'runtime_id'))
    except Exception:
        state['stale'] = True
    state.update(state='done', finished_at=int(time.time()))
    publish()


def start(key='all'):
    if key != 'all' and key not in TARGETS:
        raise ValueError('Unknown target')
    path = RUN / 'connectivity.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    started = data.get('started_at') if isinstance(data, dict) else None
    now = time.time()
    if (isinstance(data, dict) and data.get('state') == 'running' and
            type(started) in (int, float) and 0 <= started <= now + 5 and now - started < 25):
        return
    subprocess.Popen([sys.executable, __file__, key], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


if __name__ == '__main__':
    os.umask(0o077)
    worker(sys.argv[1] if len(sys.argv) == 2 else 'all')
