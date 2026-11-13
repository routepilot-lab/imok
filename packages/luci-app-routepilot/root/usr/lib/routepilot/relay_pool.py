"""Ordered PROXYIP candidates. Independent forced probes; no main UCI writes."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from runtime import RUN, atomic, read_config
import worker_backend as w

PRIVATE = Path("/etc/routepilot/backup")
POOL = PRIVATE / "relay-pool.json"
INPUT = RUN / "relay-input"
TEST = RUN / "relay-tests.json"
OBSERVED = RUN / "relay-observed.json"
BUILD = "cmliu-a8ab111-ordered-relays-v2"
PROFILE = "cf-relay-v1"
TARGETS = ("https://www.cloudflare.com/cdn-cgi/trace", "https://speed.cloudflare.com/cdn-cgi/trace")
UPSTREAM = (
    "https://github.com/cmliu/edgetunnel/blob/a8ab11125ece9bc27983f609a6359be81b21050c/README.md"
)
DEFAULT = "proxyip.cmliussss.net:443"


class RelayError(ValueError):
    pass


class ProbeDeadline(BaseException):
    pass


def address(raw):
    if not isinstance(raw, str):
        raise RelayError("请填写主机:端口")
    raw = raw.strip().lower()
    parts = raw.rsplit(":", 1)
    if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 65535:
        raise RelayError("PROXYIP 格式为公网主机:端口，不是网页 URL")
    try:
        host = w.host(parts[0])
    except ValueError:
        raise RelayError("PROXYIP 仅接受公网 IPv4 或域名，不接受内网与回环地址") from None
    return host + ":" + str(int(parts[1]))


def spec(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 6:
        raise RelayError("请保留 1～6 个候选")
    result = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) - {
            "id",
            "address",
            "source",
            "source_url",
            "enabled",
        }:
            raise RelayError("候选字段无效")
        endpoint = address(raw.get("address"))
        if endpoint in seen:
            raise RelayError("同一地址无需重复添加")
        seen.add(endpoint)
        enabled = raw.get("enabled", True)
        if type(enabled) is not bool:
            raise RelayError("启用状态无效")
        source = raw.get("source", "用户提供")
        if (
            not isinstance(source, str)
            or not 1 <= len(source) <= 120
            or any(ord(x) < 32 for x in source)
        ):
            raise RelayError("请填写简短的公开来源说明")
        link = raw.get("source_url", "")
        if link:
            if not isinstance(link, str) or len(link) > 512:
                raise RelayError("来源链接过长")
            url = urlsplit(link)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
            ):
                raise RelayError("来源链接只接受不带账户、密码或查询参数的公开 HTTPS 页面")
            try:
                w.host(url.hostname)
            except ValueError:
                raise RelayError("来源链接主机无效") from None
        result.append(
            dict(
                id=hashlib.sha256(endpoint.encode()).hexdigest()[:12],
                address=endpoint,
                source=source,
                source_url=link,
                enabled=enabled,
            )
        )
    if not any(r["enabled"] for r in result):
        raise RelayError("至少启用一个候选")
    return result


def load():
    if POOL.exists():
        return spec(json.loads(POOL.read_text())["candidates"])
    legacy = PRIVATE / "cloud.json"
    if legacy.exists():
        data = json.loads(legacy.read_text())
        old = [x.strip() for x in re.split("[,\r\n]+", str(data.get("proxyip", ""))) if x.strip()]
        if old and old != [DEFAULT]:
            return spec(
                [
                    dict(
                        address=x, source="旧云端配置迁入（用户填写）", source_url="", enabled=True
                    )
                    for x in old
                ]
            )
    return spec(
        [
            dict(
                address=DEFAULT,
                source="cmliu 上游文档示例；未作维护方背书",
                source_url=UPSTREAM,
                enabled=True,
            )
        ]
    )


def metadata():
    try:
        return dict(
            candidates=load(),
            saved=POOL.exists(),
            scope="workers-only",
            order_hash=hashlib.sha256(canonical(load()).encode()).hexdigest(),
        )
    except (OSError, ValueError, KeyError):
        return dict(candidates=[], saved=False, error="候选文件无效，请重新保存")


def canonical(rows):
    return ",".join(r["address"] for r in spec(rows) if r["enabled"])


def idle():
    if read_config()["main"].get("backend", "masque") == "worker":
        raise RelayError("请先切回主线路，再修改备用候选顺序")


def save(request_id=None):
    from request_input import claim_request

    with w.guard(), claim_request(INPUT, request_id, 16384) as data:
        idle()
        if not isinstance(data, dict) or set(data) != {"candidates"}:
            raise RelayError("候选输入格式无效")
        rows = spec(data["candidates"])
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        PRIVATE.chmod(0o700)
        atomic(POOL, json.dumps(dict(candidates=rows, saved_at=time.time()), ensure_ascii=False))
        return dict(
            saved=True,
            request_id=request_id.split(":")[0],
            candidates=rows,
            order_hash=hashlib.sha256(canonical(rows).encode()).hexdigest(),
            message="候选及顺序已保存；尚未应用到备用线路",
        )


def with_pool(node, endpoints, force=False):
    endpoints = [address(x) for x in endpoints]
    if not 1 <= len(endpoints) <= 6 or force and len(endpoints) != 1:
        raise RelayError("强制检测只能指定一个候选")
    u = urlsplit(node["path"])
    query = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k not in ("rp_relays", "rp_force", "rp_probe", "proxyip")
    ]
    query.append(("rp_relays", ",".join(endpoints)))
    if force:
        query.extend([("rp_force", "1"), ("rp_probe", "1")])
    return w.validate({**node, "path": urlunsplit(("", "", u.path, urlencode(query), ""))})


def base_fingerprint(node):
    return w.relay_fingerprint(node)


def request(node, path, authenticated=False):
    endpoint = w.pinned(node)
    args = [
        "curl",
        "-q",
        "-4",
        "-sS",
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--proto",
        "=https",
        "--connect-timeout",
        "4",
        "--max-time",
        "10",
        "--max-filesize",
        "32768",
        "--resolve",
        node["sni"] + ":443:" + endpoint["address"],
        "--config",
        "-",
        "https://" + node["sni"] + path,
    ]
    headers = 'header = "Authorization: Bearer ' + node["uuid"] + '"\n' if authenticated else ""
    r = subprocess.run(
        args,
        input=headers,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=12,
    )
    if r.returncode:
        raise RelayError("无法完成 Worker 版本检查；未更换任何候选")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise RelayError("当前 Worker 不支持候选检测；请先重新部署新版模板") from None


def require_support(node):
    data = request(node, "/health")
    if (
        not isinstance(data, dict)
        or data.get("service") != "routepilot-worker"
        or data.get("build") != BUILD
        or data.get("forced_probe") is not True
    ):
        raise RelayError("当前 Worker 模板不支持强制 PROXYIP 检测；请先按新版模板重新部署")


def row_current(row, node, now=None):
    try:
        age = (w.uptime() if now is None else now) - float(row.get("finished_uptime", -1e9))
    except (AttributeError, ValueError, TypeError):
        return False
    return (
        isinstance(row, dict)
        and row.get("fingerprint") == base_fingerprint(node)
        and row.get("boot") == w.boot_id()
        and 0 <= age <= 300
    )


def eligible(row, node):
    return (
        row_current(row, node)
        and row.get("profile") == PROFILE
        and row.get("forced") is True
        and len(row.get("checks", [])) == 2
        and all(c.get("ok") is True for c in row["checks"])
    )


def test(lock_fd):
    from worker_scan import measure

    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = dict(state="running", started_at=time.time(), results=[])

        def publish():
            atomic(TEST, json.dumps(state, ensure_ascii=False), 0o644)

        def stop(*_):
            raise ProbeDeadline("relay probe deadline")

        signal.signal(signal.SIGALRM, stop)
        signal.signal(signal.SIGTERM, stop)
        signal.alarm(240)
        publish()
        try:
            node = w.load()
            rows = load()
            state.update(fingerprint=base_fingerprint(node), total=len(rows))
            require_support(node)
            with tempfile.TemporaryDirectory(prefix="relay-probe-", dir=RUN) as folder:
                for entry in rows:
                    p = None
                    row = dict(
                        id=entry["id"],
                        address=entry["address"],
                        fingerprint=base_fingerprint(node),
                        boot=w.boot_id(),
                        checks=[],
                        forced=True,
                        profile=PROFILE,
                    )
                    try:
                        candidate = with_pool(node, [entry["address"]], True)
                        with socket.socket() as sock:
                            sock.bind(("127.0.0.1", 0))
                            port = sock.getsockname()[1]
                        cfg = Path(folder) / "core.json"
                        atomic(cfg, json.dumps(w.config(w.pinned(candidate), port)))
                        p = w.spawn(["xray", "run", "-config", str(cfg)])
                        w.ready(p, port)
                        for url in TARGETS:
                            row["checks"].append(measure(port, url))
                        row["download"] = measure(
                            port, "https://speed.cloudflare.com/__down?bytes=65536", 65536
                        )
                        row["state"] = "ok" if all(c["ok"] for c in row["checks"]) else "failed"
                        row["message"] = (
                            "此候选强制转发两个 Cloudflare 目标通过"
                            if row["state"] == "ok"
                            else "此候选未通过，不会被其他候选或直连掩盖"
                        )
                    except Exception as e:
                        row.update(
                            state="failed",
                            message=str(e) if isinstance(e, RelayError) else "该候选检测未完成",
                        )
                    finally:
                        if p:
                            w.stop_process(p)
                    row.update(finished_at=time.time(), finished_uptime=w.uptime())
                    state["results"].append(row)
                    publish()
            if base_fingerprint(w.load()) != base_fingerprint(node):
                raise RelayError("备用节点已改变，检测结果不适用于新节点")
            state.update(state="done", message="逐项检测结束；可选择已通过的候选并按顺序应用")
        except ProbeDeadline:
            state.update(
                state="failed", message="逐项检测已超时或取消；临时进程已清理，现有线路不变"
            )
        except Exception as e:
            state.update(
                state="failed",
                message=str(e) if isinstance(e, RelayError) else "检测未完成；现有线路不变",
            )
        finally:
            signal.alarm(0)
        state["finished_at"] = time.time()
        publish()


def start():
    w.load()
    load()
    with (RUN / "relay-test.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(state="running")
        atomic(
            TEST,
            json.dumps(
                dict(
                    state="queued",
                    started_at=time.time(),
                    message="强制逐项检测已提交；不接管设备",
                    results=[],
                )
            ),
            0o644,
        )
        subprocess.Popen(
            [sys.executable, __file__, str(lock.fileno())],
            pass_fds=(lock.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return dict(state="submitted")


def apply():
    with w.guard():
        idle()
        node = w.load()
        rows = [r for r in load() if r["enabled"]]
        try:
            proof = json.loads(TEST.read_text())
            results = {r["address"]: r for r in proof.get("results", [])}
        except (OSError, ValueError, KeyError):
            raise RelayError("请先完成当前节点的候选逐项检测") from None
        if proof.get("state") != "done":
            raise RelayError("请先等待当前候选检测完成")
        if any(not eligible(results.get(r["address"]), node) for r in rows):
            raise RelayError("所有启用候选都需在本次开机 5 分钟内通过强制检测；失败项可先取消启用")
        require_support(node)
        chosen = with_pool(node, [r["address"] for r in rows])
        atomic(w.NODE, json.dumps(chosen))
        (PRIVATE / "last-entry.json").unlink(missing_ok=True)
        atomic(
            w.RUN / "backup-node.json",
            json.dumps(
                dict(configured=True, fingerprint=w.fingerprint(chosen), **w.metadata(chosen)),
                ensure_ascii=False,
            ),
            0o644,
        )
        return dict(
            saved=True, message="主用 / 备用顺序已应用到备用节点；主线路不变，启用前请重新独立检测"
        )


def runtime_status():
    node = w.load()
    require_support(node)
    data = request(node, "/relay-status", True)
    allowed = {r["address"] for r in load()}
    observed = {}
    if isinstance(data, dict) and isinstance(data.get("observed"), dict):
        for key, value in data["observed"].items():
            if key not in allowed or not isinstance(value, dict):
                continue
            observed[key] = {
                k: v
                for k, v in value.items()
                if k in ("successes", "failures", "retryAt", "at") and type(v) in (int, float)
            }
            observed[key]["last"] = (
                value.get("last")
                if value.get("last") in ("connected", "connect_failed", "stream_failed")
                else "unknown"
            )
    result = dict(
        state="done",
        scope="current-isolate",
        observed=observed,
        time=time.time(),
        message="仅为此次查询命中的 Worker 实例，不能代表所有连接或全局统计",
    )
    atomic(OBSERVED, json.dumps(result, ensure_ascii=False), 0o644)
    return dict(state="done")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) != 2:
        raise SystemExit(1)
    test(int(sys.argv[1]))
