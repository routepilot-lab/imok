"""Router-owned Workers deployment. Fixed API origin; no main-route writes.

Cloud credentials and remote ownership journal are private. Public state is an
explicit projection. Never replace existing scripts/domains outside our journal.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlencode

from runtime import RUN, atomic, read_config
import worker_backend as w

PRIVATE = Path("/etc/routepilot/backup")
SETTINGS = PRIVATE / "cloud.json"
JOURNAL = PRIVATE / "cloud-owned.json"
PENDING = PRIVATE / "cloud-node.json"
INPUT = RUN / "cloud-input"
STATE = RUN / "cloud-state.json"
BUNDLE = Path("/usr/share/routepilot/workers")
API = "https://api.cloudflare.com/client/v4"


class CloudError(ValueError):
    def __init__(self, message, category="configuration", code=None):
        super().__init__(message)
        self.category = category
        self.code = code


def public(state):
    atomic(STATE, json.dumps(state, ensure_ascii=False), 0o644)


def directory_state():
    return RUN / "cloud-directory-state.json"


def directory_metadata(cfg):
    """Credential-scoped directory, separate from deployment/job readiness."""
    path = PRIVATE / "cloud-directory.json"
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 262144:
            return None
        data = json.loads(path.read_text())
        if data.get("credential") != hashlib.sha256(cfg["token"].encode()).hexdigest():
            return None
        rows = data.get("rows")
        stamp = data.get("read_at")
        if not isinstance(rows, list) or not isinstance(stamp, (float, int)):
            return None
        accounts = {r["account_id"]: dict(id=r["account_id"], name=r["account_name"]) for r in rows}
        chosen = cfg["account_id"] or (next(iter(accounts)) if len(accounts) == 1 else "")
        return dict(
            state="cached",
            read_at=stamp,
            stale=not 0 <= time.time() - stamp <= 900,
            accounts=list(accounts.values()),
            discovered_account_id=chosen,
            all_zones=[{k: r[k] for k in ("id", "name", "account_id")} for r in rows],
            zones=[{k: r[k] for k in ("id", "name")} for r in rows if r["account_id"] == chosen],
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def publish_job(action, state):
    if action == "verify":
        atomic(directory_state(), json.dumps(state, ensure_ascii=False), 0o644)
    else:
        public(state)


def valid_settings(data, old=None):
    if not isinstance(data, dict) or set(data) - {
        "token",
        "account_id",
        "script",
        "hostname",
        "zone_id",
        "proxyip",
    }:
        raise CloudError("云端配置字段无效")
    old = old or {}
    token = data.get("token") or old.get("token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
        raise CloudError("请填写受限 Cloudflare API 令牌")
    account = data.get("account_id", "")
    zone = data.get("zone_id", "")
    if (account and not re.fullmatch("[a-fA-F0-9]{32}", str(account))) or (
        zone and not re.fullmatch("[a-fA-F0-9]{32}", str(zone))
    ):
        raise CloudError("账户 ID / 区域 ID 应为 32 位字符")
    name = data.get("script", "")
    if not isinstance(name, str) or not re.fullmatch("routepilot-[a-z0-9][a-z0-9-]{0,40}", name):
        raise CloudError("Worker 名称需以 routepilot- 开头，仅用小写字母、数字和连字符")
    host = ""
    if data.get("hostname"):
        try:
            host = w.host(data["hostname"])
        except ValueError:
            raise CloudError("请填写有效子域名，不含 https://、端口或路径") from None
        if not w.HOST.fullmatch(host):
            raise CloudError("请填写子域名，不使用 IP 地址")
    from relay_pool import address

    values = [
        x.strip()
        for x in re.split("[,\r\n]+", str(data.get("proxyip") or "proxyip.cmliussss.net:443"))
        if x.strip()
    ]
    if not 1 <= len(values) <= 6:
        raise CloudError("PROXYIP 仅支持 1～6 个候选")
    try:
        relay = ",".join(dict.fromkeys(address(x) for x in values))
    except ValueError:
        raise CloudError("PROXYIP 候选格式应为公网主机:端口") from None
    return dict(
        token=token,
        account_id=account.lower(),
        script=name,
        hostname=host,
        zone_id=zone.lower(),
        proxyip=relay,
    )


def load_settings():
    if SETTINGS.is_symlink() or SETTINGS.stat().st_mode & 0o077 or SETTINGS.stat().st_size > 8192:
        raise CloudError("云端凭据文件权限或大小无效")
    return valid_settings(json.loads(SETTINGS.read_text()))


def idle_backup():
    if read_config()["main"].get("backend", "masque") == "worker":
        raise CloudError("请先手动切回主线路，再修改备用部署或入口")


@contextmanager
def idle_job():
    with (RUN / "cloud-job.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CloudError("云端任务正在运行，请完成后再修改") from None
        yield


def verify_account_team(cfg):
    from client_provider import ClientError, organization

    try:
        team = organization(read_config()["main"].get("organization"))
    except ClientError:
        raise CloudError("请先在「账户与协议」保存本机的 Cloudflare 团队名称") from None
    try:
        result = api(cfg, "GET", "/accounts/" + cfg["account_id"] + "/access/organizations")
    except CloudError as error:
        if error.code == 403:
            raise CloudError(
                "首次绑定账户需要令牌有本账户的 Zero Trust 组织读取权限（Access: Organizations, Identity Providers, and Groups Read）",
                error.category,
                error.code,
            ) from None
        raise
    domain = result.get("auth_domain") if isinstance(result, dict) else None
    expected = team + ".cloudflareaccess.com"
    if (
        not isinstance(domain, str)
        or domain.strip().lower().removeprefix("https://").removesuffix("/") != expected
    ):
        raise CloudError(
            "此 Cloudflare 账户的 Zero Trust 团队与「账户与协议」中的团队不一致，授权未保存"
        )


def save(request_id=None):
    from request_input import claim_request

    with idle_job(), w.guard(), claim_request(INPUT, request_id, 8192) as payload:
        idle_backup()
        old = load_settings() if SETTINGS.exists() else {}
        cfg = valid_settings(payload, old)
        if cfg["account_id"] and cfg["account_id"] != old.get("account_id"):
            verify_account_team(cfg)
        PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        PRIVATE.chmod(0o700)
        atomic(SETTINGS, json.dumps(cfg))
        if old.get("token") != cfg["token"]:
            (PRIVATE / "cloud-directory.json").unlink(missing_ok=True)
            directory_state().unlink(missing_ok=True)
        if cfg != old:
            public(
                dict(
                    state="configured",
                    message="凭据已私密保存；尚未创建或修改云端资源",
                    **metadata(cfg),
                )
            )
        return dict(saved=True, request_id=request_id.split(":")[0])


def forget():
    with idle_job(), w.guard():
        SETTINGS.unlink(missing_ok=True)
        (PRIVATE / "cloud-directory.json").unlink(missing_ok=True)
        directory_state().unlink(missing_ok=True)
        public(
            dict(
                state="unconfigured",
                configured=False,
                message="已清除本机 API 凭据；已部署线路继续保留。撤销令牌请到 Cloudflare。",
            )
        )
        return dict(removed=True)


def project_metadata(cfg=None):
    """Local deployment journal only; never project UUIDs, tokens or ownership secrets."""
    try:
        records = registry()
        if not isinstance(records, dict) or len(records) > 20:
            return []
    except (OSError, ValueError, TypeError):
        return []
    try:
        node = w.load()
    except (OSError, ValueError, TypeError):
        node = {}
    rows = []
    for key, record in records.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        account, sep, script = key.partition("/")
        if (
            not sep
            or not re.fullmatch("[a-f0-9]{32}", account)
            or not re.fullmatch("routepilot-[a-z0-9][a-z0-9-]{0,40}", script)
        ):
            continue
        try:
            hostname = w.host(record.get("hostname"))
        except ValueError:
            continue
        rows.append(
            dict(
                script=script,
                hostname=hostname,
                uploaded=record.get("uploaded") is True,
                bound=bool(record.get("domain_id")),
                selected=bool(cfg and own_key(cfg) == key),
                saved_as_backup=bool(
                    node and node.get("uuid") == record.get("uuid") and node.get("sni") == hostname
                ),
            )
        )
    return sorted(
        rows, key=lambda row: (not row["saved_as_backup"], not row["selected"], row["hostname"])
    )


def metadata(cfg=None):
    try:
        cfg = cfg or load_settings()
    except (OSError, ValueError, TypeError):
        return dict(configured=False, projects=project_metadata(), projects_read_at=time.time())
    return dict(
        configured=True,
        token_saved=True,
        directory=directory_metadata(cfg),
        projects=project_metadata(cfg),
        projects_read_at=time.time(),
        **{k: cfg[k] for k in ("account_id", "script", "hostname", "zone_id", "proxyip")},
    )


def identity(cfg):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in cfg.items() if k not in ("token", "proxyip")}, sort_keys=True
        ).encode()
    ).hexdigest()


def api(cfg, method, endpoint, payload=None, module=False, missing=False):
    """Token goes to curl stdin, not argv/environment. No redirect/host injection."""
    if not endpoint.startswith("/") or "://" in endpoint or "\n" in endpoint:
        raise CloudError("API 路径无效")
    with tempfile.TemporaryDirectory(prefix="cloud-api-", dir=RUN) as folder:
        folder = Path(folder)
        reply = folder / "reply"
        atomic(reply, "")
        args = [
            "curl",
            "-q",
            "-4",
            "-sS",
            "--globoff",
            "--proxy",
            "",
            "--noproxy",
            "*",
            "--proto",
            "=https",
            "--connect-timeout",
            "8",
            "--max-time",
            "30",
            "--max-filesize",
            "8388608",
            "--config",
            "-",
            "-X",
            method,
            "-o",
            str(reply),
            "-w",
            "%{http_code}",
        ]
        if module:
            manifest = json.loads((BUNDLE / "SOURCE.json").read_text())
            if (
                hashlib.sha256((BUNDLE / "worker.mjs").read_bytes()).hexdigest()
                != manifest["template_sha256"]
            ):
                raise CloudError("内置 Workers 模板校验失败，未上传")
            body = folder / "metadata"
            atomic(body, json.dumps(payload))
            args += [
                "--form",
                "metadata=@" + str(body) + ";type=application/json",
                "--form",
                "worker.mjs=@" + str(BUNDLE / "worker.mjs") + ";type=application/javascript+module",
            ]
        elif payload is not None:
            body = folder / "body"
            atomic(body, json.dumps(payload))
            args += ["-H", "Content-Type: application/json", "--data-binary", "@" + str(body)]
        args.append(API + endpoint)
        result = subprocess.run(
            args,
            input='header = "Authorization: Bearer ' + cfg["token"] + '"\n',
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=35,
        )
        code = int(result.stdout) if result.stdout.isdigit() else 0
        if result.returncode:
            raise CloudError("Cloudflare API 连接未完成，请检查路由器直连网络")
        if code == 404 and missing:
            return None
        if code == 401:
            raise CloudError("Cloudflare 令牌无效或已到期，请核对授权", "authentication", 401)
        if code == 403:
            raise CloudError(
                "Cloudflare 拒绝访问（403）：可能缺少操作权限或账户受限，请核对控制台通知",
                "access_denied",
                403,
            )
        if code == 429:
            raise CloudError("Cloudflare API 请求限流，请稍后重试同一项目", "rate_limit", 429)
        try:
            data = json.loads(reply.read_text())
        except (ValueError, OSError):
            raise CloudError("Cloudflare API 响应无效") from None
        if not 200 <= code < 300 or data.get("success") is not True:
            error = data.get("errors") or [{}]
            number = error[0].get("code")
            from cloud_health import api_hint

            message, category = api_hint(number)
            raise CloudError(message, category, number if type(number) is int else None)
        return data.get("result")


def zone_rows(cfg):
    rows = []
    for page in range(1, 5):
        query = {"status": "active", "per_page": 50, "page": page}
        if cfg["account_id"]:
            query["account.id"] = cfg["account_id"]
        batch = api(cfg, "GET", "/zones?" + urlencode(query))
        if not isinstance(batch, list):
            raise CloudError("域名列表格式无效")
        for row in batch:
            account = row.get("account", {})
            if (
                not re.fullmatch("[a-f0-9]{32}", str(row.get("id", "")))
                or not re.fullmatch("[a-f0-9]{32}", str(account.get("id", "")))
                or (cfg["account_id"] and account["id"] != cfg["account_id"])
                or row.get("status", "active") != "active"
            ):
                continue
            try:
                name = w.host(row.get("name"))
            except ValueError:
                continue
            rows.append(
                dict(
                    id=row["id"],
                    name=name,
                    account_id=account["id"],
                    account_name=str(account.get("name") or account["id"][:8])[:120],
                )
            )
        if len(batch) < 50:
            return sorted(rows, key=lambda r: r["name"])
    raise CloudError("可用区域超过首批支持范围，请缩小令牌的区域权限")


def zones(cfg):
    if not cfg["account_id"]:
        raise CloudError("请先读取并选择 Cloudflare 账户")
    return [{k: r[k] for k in ("id", "name")} for r in zone_rows(cfg)]


def zone_for(cfg, rows):
    row = next((r for r in rows if r["id"] == cfg["zone_id"]), None)
    if not row or not cfg["hostname"].endswith("." + row["name"]):
        raise CloudError("请选择子域名所属的区域；不能绑定根域名或其他账户域名")
    return row


def registry():
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}


def own_key(cfg):
    return cfg["account_id"] + "/" + cfg["script"]


def matching_owner(settings, record):
    return isinstance(settings, dict) and any(
        b.get("name") == "ROUTEPILOT_OWNER" and b.get("text") == record.get("owner")
        for b in settings.get("bindings", [])
    )


def health(hostname):
    from cloud_health import probe

    return probe(hostname, subprocess.run)


def deployment_health(hostname):
    result = health(hostname)
    if result is True:
        return dict(state="ready", message="域名 HTTPS 已就绪；设为备用后仍需独立检测")
    if result is False:
        return dict(
            state="waiting_dns", message="域名已绑定，DNS 或证书仍在生效，请稍后检查同一部署"
        )
    return result


def deploy(cfg, publish):
    idle_backup()
    rows = zones(cfg)
    zone = zone_for(cfg, rows)
    account = "/accounts/" + cfg["account_id"] + "/workers"
    publish("checking", "正在核对项目、域名归属与冲突", zones=rows)
    records = registry()
    key = own_key(cfg)
    record = records.get(key)
    existing = api(cfg, "GET", account + "/scripts/" + cfg["script"] + "/settings", missing=True)
    if existing is not None and (not record or not matching_owner(existing, record)):
        raise CloudError("该 Worker 名称已被其他项目使用；请选择新名称，不覆盖原项目")
    if record and record.get("hostname") != cfg["hostname"]:
        raise CloudError("该托管项目已有其他域名，请换一个新的 routepilot- 名称")
    domains = api(cfg, "GET", account + "/domains?" + urlencode({"hostname": cfg["hostname"]}))
    if not isinstance(domains, list):
        raise CloudError("域名绑定列表格式无效")
    binding = next((x for x in domains if x.get("hostname") == cfg["hostname"]), None)
    if binding and (not record or binding.get("service") != cfg["script"]):
        raise CloudError("此域名已绑定其他服务；不会覆盖，请使用新的子域名")
    dns = api(
        cfg,
        "GET",
        "/zones/"
        + zone["id"]
        + "/dns_records?"
        + urlencode({"name": cfg["hostname"], "per_page": 100}),
    )
    if not isinstance(dns, list):
        raise CloudError("DNS 列表格式无效")
    if dns and not binding:
        raise CloudError("此子域名已有 DNS 记录；不会覆盖，请换一个尚未使用的子域名")
    if record is None:
        if len(records) >= 20:
            raise CloudError("本机托管项目已达 20 个，请先整理项目")
        record = dict(
            owner=uuid.uuid4().hex,
            uuid=str(uuid.uuid4()),
            hostname=cfg["hostname"],
            zone_id=zone["id"],
        )
        records[key] = record
        atomic(JOURNAL, json.dumps(records))  # reserve identity before a retryable upload
    publish("uploading", "正在创建 / 更新本机托管 Worker")
    bindings = [
        dict(name="UUID", type="secret_text", text=record["uuid"]),
        dict(name="PROXYIP", type="plain_text", text=cfg["proxyip"]),
        dict(name="ROUTEPILOT_OWNER", type="plain_text", text=record["owner"]),
    ]
    idle_backup()
    api(
        cfg,
        "PUT",
        account + "/scripts/" + cfg["script"],
        dict(main_module="worker.mjs", compatibility_date="2026-10-01", bindings=bindings),
        module=True,
    )
    record["uploaded"] = True
    atomic(JOURNAL, json.dumps(records))
    publish("binding", "正在绑定子域名；证书和 DNS 由 Cloudflare 创建")
    idle_backup()
    bound = api(
        cfg,
        "PUT",
        account + "/domains",
        dict(hostname=cfg["hostname"], service=cfg["script"], zone_id=zone["id"], enabled=True),
    )
    if (
        not isinstance(bound, dict)
        or bound.get("hostname") != cfg["hostname"]
        or bound.get("service") != cfg["script"]
    ):
        raise CloudError("域名绑定尚未确认；保留已创建项目，可重新检查")
    record["domain_id"] = bound["id"]
    atomic(JOURNAL, json.dumps(records))
    publish("waiting_dns", "域名已绑定，正在等待 DNS 与 HTTPS 证书生效")
    node = w.validate(
        dict(
            name="Workers · " + cfg["hostname"],
            uuid=record["uuid"],
            address=cfg["hostname"],
            sni=cfg["hostname"],
            port=443,
            path="/tunnel",
        )
    )
    atomic(PENDING, json.dumps(dict(identity=identity(cfg), node=node)))
    return dict(**deployment_health(cfg["hostname"]), zones=rows, identity=identity(cfg))


def check_target(cfg):
    rows = zones(cfg)
    zone = zone_for(cfg, rows)
    account = "/accounts/" + cfg["account_id"] + "/workers"
    record = registry().get(own_key(cfg))
    existing = api(cfg, "GET", account + "/scripts/" + cfg["script"] + "/settings", missing=True)
    if existing is not None and (not record or not matching_owner(existing, record)):
        return dict(
            state="conflict",
            message="Worker 名称已被其他项目使用，请改一个 routepilot- 名称",
            zones=rows,
        )
    bindings = api(cfg, "GET", account + "/domains?" + urlencode({"hostname": cfg["hostname"]}))
    if not isinstance(bindings, list):
        raise CloudError("域名绑定列表格式无效")
    binding = next((x for x in bindings if x.get("hostname") == cfg["hostname"]), None)
    if binding and (not record or binding.get("service") != cfg["script"]):
        return dict(
            state="conflict", message="此子域名已绑定其他服务；请换一个子域名前缀", zones=rows
        )
    records = api(
        cfg,
        "GET",
        "/zones/"
        + zone["id"]
        + "/dns_records?"
        + urlencode({"name": cfg["hostname"], "per_page": 100}),
    )
    if not isinstance(records, list):
        raise CloudError("DNS 列表格式无效")
    if records and not binding:
        return dict(
            state="conflict", message="此子域名已有 DNS 记录；不会覆盖，请换一个前缀", zones=rows
        )
    return dict(
        state="available",
        message="当前名称和子域名检查通过；尚未创建或修改资源，部署时会再次核对",
        zones=rows,
    )


def verify(cfg):
    rows = zone_rows({**cfg, "account_id": ""})
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic(
        PRIVATE / "cloud-directory.json",
        json.dumps(
            dict(
                credential=hashlib.sha256(cfg["token"].encode()).hexdigest(),
                read_at=time.time(),
                rows=rows,
            )
        ),
    )
    return dict(
        **{**directory_metadata(cfg), "state": "verified"},
        message="已读取当前令牌授权范围内的账户和根域名；未保存表单、部署或修改绑定",
    )


def check(cfg):
    record = registry().get(own_key(cfg))
    if not record or record.get("hostname") != cfg["hostname"] or not record.get("domain_id"):
        raise CloudError("当前设置尚无本机完成的域名绑定，请先一键部署")
    account = "/accounts/" + cfg["account_id"] + "/workers"
    settings = api(cfg, "GET", account + "/scripts/" + cfg["script"] + "/settings")
    domains = api(cfg, "GET", account + "/domains?" + urlencode({"hostname": cfg["hostname"]}))
    if not matching_owner(settings, record) or not any(
        d.get("id") == record["domain_id"]
        and d.get("hostname") == cfg["hostname"]
        and d.get("service") == cfg["script"]
        for d in domains
    ):
        raise CloudError("远端项目或域名归属已改变，未使用此节点")
    return dict(**deployment_health(cfg["hostname"]), identity=identity(cfg))


def promote():
    with idle_job(), w.guard():
        idle_backup()
        cfg = load_settings()
        data = json.loads(PENDING.read_text())
        state = json.loads(STATE.read_text())
        if (
            data.get("identity") != identity(cfg)
            or state.get("identity") != identity(cfg)
            or state.get("state") != "ready"
            or check(cfg).get("state") != "ready"
        ):
            raise CloudError("请先确认当前部署的域名 HTTPS 已就绪")
        node = w.validate(data["node"])
        atomic(w.NODE, json.dumps(node))
        (PRIVATE / "last-entry.json").unlink(missing_ok=True)
        atomic(
            w.RUN / "backup-node.json",
            json.dumps(
                dict(configured=True, fingerprint=w.fingerprint(node), **w.metadata(node)),
                ensure_ascii=False,
            ),
            0o644,
        )
        return dict(
            saved=True,
            message="新部署已设为备用节点，未启用；请为新节点逐项检测并应用 PROXYIP 顺序，再做整条线路独立检测",
        )


def job(action, lock_fd):
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.time()
        state = dict(state="running", action=action, started_at=started)

        def publish(phase, message, **extra):
            state.update(phase=phase, message=message, **extra)
            publish_job(action, state)

        try:
            cfg = load_settings()
            state.update(metadata(cfg))
            publish("connecting", "正在连接 Cloudflare API")
            result = (
                deploy(cfg, publish)
                if action == "deploy"
                else check(cfg)
                if action == "check"
                else check_target(cfg)
                if action == "target"
                else verify(cfg)
            )
            # Never turn a successful operation for old settings into proof for new settings.
            if identity(load_settings()) != identity(cfg):
                raise CloudError("设置已变化，请重新检查当前配置")
            state.update(metadata(cfg))
            state.update(result)
        except Exception as error:
            state.update(
                state="failed",
                message=str(error)
                if isinstance(error, CloudError)
                else "云端操作未完成；未切换线路，已保留可重试的项目记录",
            )
            if isinstance(error, CloudError):
                state.update(error_category=error.category, error_code=error.code)
        state["finished_at"] = time.time()
        publish_job(action, state)


def start(action):
    if action not in ("verify", "deploy", "check", "target"):
        raise CloudError("未知云端操作")
    load_settings()
    with (RUN / "cloud-job.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(state="running")
        publish_job(
            action,
            dict(
                state="queued",
                action=action,
                started_at=time.time(),
                message="域名目录读取已提交；表单和绑定不变"
                if action == "verify"
                else "云端任务已提交；现有主线路不变",
            ),
        )
        subprocess.Popen(
            [sys.executable, __file__, action, str(lock.fileno())],
            pass_fds=(lock.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return dict(state="submitted")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) != 3 or sys.argv[1] not in ("verify", "deploy", "check", "target"):
        raise SystemExit(1)
    job(sys.argv[1], int(sys.argv[2]))
