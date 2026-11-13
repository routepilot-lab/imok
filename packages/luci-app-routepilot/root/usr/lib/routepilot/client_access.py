"""Owned portable client identities; no main UCI, warp-cli, routing, or services.

All credentials stay in this module's 0700 directory. Read-only RPC is a separate
executable, and its result never contains private keys, device tokens or JWTs.
"""

from contextlib import contextmanager
import base64
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

import client_provider as provider
import client_subscriptions as subscriptions
from client_provider import ClientError
from runtime import atomic, read_config, RUN
from request_input import claim_request, reap_requests, prepare_requests

PRIVATE = Path("/etc/routepilot/clients")
VOLATILE = RUN / "client-jobs"
OWNER = "imok-client-access-v1"
MAX_CLIENTS = 32
BUSY = (
    "queued",
    "registering",
    "enrolling",
    "refreshing",
    "selecting_exit",
    "publishing_subscription",
    "stopping_subscription",
    "revoking",
)
LABELS = {
    "draft": "尚未注册",
    "queued": "等待执行",
    "registering": "正在注册",
    "enrolling": "正在核对设备密钥",
    "registered": "注册信息已核验",
    "refreshing": "正在同步凭据与出口",
    "selecting_exit": "正在应用客户端出口",
    "publishing_subscription": "正在更新私密订阅",
    "stopping_subscription": "正在停用订阅链接",
    "revoking": "正在撤销",
    "revoked": "云端注册已撤销",
    "failed": "操作失败",
    "unknown": "云端结果待核对",
    "review": "配置待核对",
}


def client_protocol(row):
    protocol = row.get("protocol", "masque")
    if protocol not in ("masque", "wireguard"):
        raise ClientError("客户端连接协议仅支持 MASQUE 或 WireGuard")
    return protocol


def directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or path.is_symlink():
        raise ClientError("客户端私有目录无效，未执行操作")
    path.chmod(0o700)


def read_private(path, limit=2097152):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            st = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_mode & 0o077
                or st.st_size > limit
            ):
                raise ValueError()
            raw = handle.read(limit + 1)
            if len(raw.encode()) > limit:
                raise ValueError()
            return json.loads(raw)
    except (OSError, ValueError, TypeError):
        raise ClientError("客户端私有记录损坏或权限异常；未重置身份") from None


def load():
    if not PRIVATE.exists() and not PRIVATE.is_symlink():
        return {"schema": 1, "clients": []}
    st = PRIVATE.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ClientError("客户端私有目录权限异常")
    path = PRIVATE / "registry.json"
    marker = PRIVATE / "initialized.json"
    initialized = marker.exists() or marker.is_symlink()
    if initialized and read_private(marker, 256) != {"schema": 1, "owner": OWNER}:
        raise ClientError("客户端初始化记录无效；未重置身份")
    if not path.exists() and not path.is_symlink():
        if initialized:
            raise ClientError("已有客户端的注册表丢失；未当作空安装重置")
        return {"schema": 1, "clients": []}
    doc = read_private(path)
    try:
        if (
            set(doc) != {"schema", "clients"}
            or type(doc["schema"]) is not int
            or doc["schema"] != 1
            or not isinstance(doc["clients"], list)
            or len(doc["clients"]) > MAX_CLIENTS
        ):
            raise ValueError()
        ids, remote_ids = set(), set()
        for row in doc["clients"]:
            ident = row["id"]
            if (
                not re.fullmatch("[a-f0-9]{32}", ident)
                or ident in ids
                or row["owner"] != OWNER
                or row["state"] not in LABELS
            ):
                raise ValueError()
            ids.add(ident)
            provider.organization(row["organization"])
            text(row["name"], "客户端名称")
            protocol = client_protocol(row)
            settings(row["settings"], protocol)
            public_network(row)
            if protocol == "wireguard" and "subscription" in row:
                raise ValueError()
            subscriptions.state(row)
            for field in (
                "created_at",
                "updated_at",
                "operation_at",
                "last_checked_at",
                "last_exported_at",
            ):
                if field in row and (
                    type(row[field]) not in (int, float) or not math.isfinite(row[field])
                ):
                    raise ValueError()
            if row.get("device_id"):
                remote = provider.registration_key(row["device_id"])
                if (
                    provider.registration_id(row["device_id"]) != row["device_id"]
                    or remote in remote_ids
                ):
                    raise ValueError()
                remote_ids.add(remote)
            if row["state"] == "registered":
                required = (
                    "device_id",
                    "device_token",
                    "private_key",
                    "public_key",
                    "peer_key",
                    "ipv4",
                )
                if any(not isinstance(row.get(k), str) or not row[k] for k in required):
                    raise ValueError()
                if protocol == "wireguard":
                    reserved = row.get("reserved")
                    if (
                        any(
                            len(base64.b64decode(row[k], validate=True)) != 32
                            for k in ("private_key", "public_key", "peer_key")
                        )
                        or not isinstance(reserved, list)
                        or len(reserved) != 3
                        or not all(type(n) is int and 0 <= n <= 255 for n in reserved)
                    ):
                        raise ValueError()
                else:
                    if (
                        len(row["private_key"]) > 4096
                        or len(base64.b64decode(row["private_key"], validate=True)) < 100
                    ):
                        raise ValueError()
                    for field in ("public_key", "peer_key"):
                        raw = base64.b64decode(row[field], validate=True)
                        if len(raw) != 91 or not raw.startswith(provider.P256_PREFIX):
                            raise ValueError()
                ipaddress.IPv4Address(row["ipv4"])
                if row.get("ipv6"):
                    ipaddress.IPv6Address(row["ipv6"])
        return doc
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ClientError("客户端归属记录无效；未执行任何云端操作") from None


def save(doc):
    atomic(PRIVATE / "registry.json", json.dumps(doc, ensure_ascii=False))
    marker = PRIVATE / "initialized.json"
    if not marker.exists():
        atomic(marker, json.dumps({"schema": 1, "owner": OWNER}))


@contextmanager
def locked(wait=0):
    directory(PRIVATE)
    fd = os.open(PRIVATE / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ClientError("客户端锁无效")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ClientError("另一个客户端操作正在进行，请稍后重试") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def find(doc, ident):
    if not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident):
        raise ClientError("客户端标识无效")
    for row in doc["clients"]:
        if row["id"] == ident and row["owner"] == OWNER:
            return row
    raise ClientError("未找到本模块创建的客户端")


def active(row):
    stamp = row.get("operation_at", 0)
    return (
        row["state"] in BUSY
        and type(stamp) in (int, float)
        and math.isfinite(stamp)
        and -5 <= time.time() - stamp < 180
    )


def effective_state(row):
    if row["state"] not in BUSY or active(row):
        return row["state"]
    # queued is persisted before a worker can write to Cloudflare. A stale
    # queued registration is retryable; registering/enrolling is not.
    if row["state"] == "queued":
        return (
            "failed"
            if row.get("operation") == "register" and not row.get("device_id")
            else "review"
        )
    return "unknown"


def public_network(row):
    value = row.get("network")
    if value is None:
        return dict(state="unread", selected_id="", selected_name="", networks=[], checked_at=0)
    if (
        not isinstance(value, dict)
        or set(value)
        - {"state", "catalog", "checked_at", "error", "requested_id", "previous_id", "applied_at"}
        or value.get("state") not in ("confirmed", "pending", "unknown", "error")
    ):
        raise ClientError("客户端出口记录无效，请核对私有记录")
    result = dict(state=value["state"], selected_id="", selected_name="", networks=[], checked_at=0)
    for key in ("checked_at", "applied_at"):
        if key in value:
            if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
                raise ClientError("客户端出口时间记录无效")
            result[key] = value[key]
    if "catalog" in value:
        catalog = provider.network_catalog(value["catalog"])
        result.update(catalog)
        result["selected_name"] = next(
            n["name"] for n in catalog["networks"] if n["id"] == catalog["selected_id"]
        )
    if value["state"] == "confirmed" and not result["selected_id"]:
        raise ClientError("客户端出口缺少云端核对结果")
    for key in ("requested_id", "previous_id"):
        if key in value:
            result[key] = provider.network_id(value[key])
    if "error" in value:
        result["error"] = text(value["error"], "出口错误说明", 300)
    return result


def network_ready(row):
    network = public_network(row)
    return network["state"] == "confirmed" and any(
        n["id"] == network["selected_id"] and not n["is_default"] for n in network["networks"]
    )


def service_auth_path(org):
    return PRIVATE / ("service-auth-" + provider.organization(org) + ".json")


def configured_team():
    try:
        return provider.organization(read_config()["main"].get("organization"))
    except ClientError:
        raise ClientError("请先在「账户与协议」保存本机的 Cloudflare 团队名称") from None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ):
        raise ClientError("暂时无法读取本机团队设置，请稍后刷新") from None


def require_system_team(org):
    if org != configured_team():
        raise ClientError("客户端团队与本机团队不一致。请为当前团队新建客户端")


def saved_service_auth(org):
    try:
        st = service_auth_path(org).lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077


def forget_service_auth(payload):
    if set(payload) != {"id"}:
        raise ClientError("清除服务授权请求无效")
    with locked():
        doc = load()
        row = find(doc, payload["id"])
        if any(active(r) and r["organization"] == row["organization"] for r in doc["clients"]):
            raise ClientError("本团队有客户端任务正在执行，请等待完成后再清除服务授权")
        service_auth_path(row["organization"]).unlink(missing_ok=True)
    return {"forgotten": True}


def public_row(row):
    state = effective_state(row)
    fields = (
        "id",
        "name",
        "organization",
        "created_at",
        "updated_at",
        "ipv4",
        "ipv6",
        "fingerprint",
        "last_checked_at",
        "last_exported_at",
        "settings",
        "operation",
        "operation_id",
        "error",
        "bootstrap_fingerprint",
        "bootstrap_serial",
    )
    result = {k: row[k] for k in fields if k in row}
    protocol = client_protocol(row)
    result["protocol"] = protocol
    result["settings"] = settings(row["settings"], protocol)
    result["service_auth_saved"] = saved_service_auth(row["organization"])
    if row["state"] == "queued" and state != "queued":
        result["error"] = "任务尚未启动即失效，未提交本次云端操作；可重新提交"
    result.update(
        registration_id=row.get("device_id", ""),
        state=state,
        label=LABELS[state],
        has_registration=bool(row.get("device_id")),
        exportable=state == "registered" and network_ready(row),
        busy=active(row),
        tunnel_verified=False,
        exit_verified=False,
        network=public_network(row),
        network_selection_verified=state == "registered" and network_ready(row),
        subscription=subscriptions.state(
            row,
            portable_uri(row)
            if protocol == "masque"
            and row.get("private_key")
            and row.get("peer_key")
            and row.get("ipv4")
            else None,
        ),
    )
    return result


def listing():
    try:
        team = {"name": configured_team(), "error": ""}
    except ClientError as error:
        team = {"name": "", "error": str(error)}
    return dict(
        schema=1,
        team=team,
        clients=[public_row(r) for r in load()["clients"]],
        capabilities={
            "shadowrocket": True,
            "vnet": True,
            "private_subscription": True,
            "service_token": True,
            "wireguard": True,
            "registration_provider": "cloudflare-client-protocol",
        },
        no_router_changes=True,
    )


def text(value, label, maximum=64):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ClientError(label + "无效")
    return value.strip()


def settings(data, protocol="masque"):
    if not isinstance(data, dict) or set(data) - {
        "endpoint",
        "port",
        "dns",
        "mtu",
        "keepalive",
        "cc",
    }:
        raise ClientError("客户端连接参数无效；出口请通过独立的出口选择操作设置")
    wireguard = protocol == "wireguard"
    default_endpoint = "162.159.193.2" if wireguard else "162.159.197.2"
    ports = provider.WIREGUARD_PORTS if wireguard else provider.PORTS
    endpoint = data.get("endpoint", default_endpoint)
    dns = data.get("dns", "162.159.36.1")
    try:
        if not isinstance(endpoint, str) or ipaddress.IPv4Address(
            endpoint
        ) not in ipaddress.IPv4Network(default_endpoint + "/24", strict=False):
            raise ValueError()
        if endpoint.endswith((".0", ".255")):
            raise ValueError()
        if not isinstance(dns, str):
            raise ValueError()
        dns = str(ipaddress.ip_address(dns))
        addr = ipaddress.ip_address(dns)
        if addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_link_local:
            raise ValueError()

        def number(key, default, low, high):
            val = data.get(key, default)
            if (
                isinstance(val, bool)
                or not isinstance(val, (str, int))
                or not re.fullmatch("[0-9]{1,5}", str(val))
            ):
                raise ValueError()
            val = int(val)
            if not low <= val <= high:
                raise ValueError()
            return val

        port = number("port", ports[0], 1, 65535)
        if port not in ports:
            raise ValueError()
        mtu = number("mtu", 1280, 1280, 1420)
        keepalive = number("keepalive", 25 if wireguard else 30, 10, 120)
        cc = data.get("cc", "cubic")
        if cc not in ("cubic", "bbr", "new_reno"):
            raise ValueError()
    except (ValueError, TypeError):
        raise ClientError("请核对官方入口 IPv4、UDP 端口、DNS、MTU 和心跳参数") from None
    return dict(endpoint=endpoint, port=port, dns=dns, mtu=mtu, keepalive=keepalive, cc=cc)


def create(payload):
    if "name" not in payload or set(payload) - {"name", "organization", "protocol"}:
        raise ClientError("新增客户端输入无效")
    name = text(payload["name"], "客户端名称")
    protocol = client_protocol(payload)
    with locked():
        doc = load()
        team = configured_team()
        if "organization" in payload and provider.organization(payload["organization"]) != team:
            raise ClientError("新增客户端统一使用「账户与协议」中的团队")
        if len(doc["clients"]) >= MAX_CLIENTS:
            raise ClientError("本机最多管理 32 个客户端，请先清理不用的记录")
        row = dict(
            id=uuid.uuid4().hex,
            owner=OWNER,
            name=name,
            organization=team,
            state="draft",
            protocol=protocol,
            created_at=time.time(),
            updated_at=time.time(),
            settings=settings({}, protocol),
        )
        doc["clients"].append(row)
        save(doc)
    return dict(client=public_row(row))


def spawn(ident, operation_id):
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "job", ident, operation_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def expire_jobs():
    if not VOLATILE.exists():
        return
    st = VOLATILE.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ClientError("客户端任务目录权限异常")
    now = time.time()
    for path in VOLATILE.iterdir():
        if re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
            age = now - path.lstat().st_mtime
            if age > 180 or age < -5:
                path.unlink(missing_ok=True)


def start(action, payload):
    allowed = {
        "register": {"id", "token", "accept_terms"},
        "refresh": {"id"},
        "select_exit": {"id", "vnet_id", "expected_vnet_id"},
        "publish_subscription": {"id", "confirm"},
        "stop_subscription": {"id", "confirm"},
        "revoke": {"id", "confirm"},
    }
    service_form = action == "register" and set(payload) == {
        "id",
        "service_auth",
        "remember_service_auth",
        "accept_terms",
    }
    if not service_form and (action not in allowed or set(payload) != allowed[action]):
        raise ClientError("客户端任务输入无效")
    with locked():
        doc = load()
        expire_jobs()
        row = find(doc, payload["id"])
        if any(active(r) for r in doc["clients"]):
            raise ClientError("已有客户端任务正在执行，请等待完成")
        if row["state"] == "queued" and not active(row):
            previous_job = row.get("operation_id", "")
            if re.fullmatch("[a-f0-9]{32}", previous_job):
                (VOLATILE / (previous_job + ".json")).unlink(missing_ok=True)
        row["state"] = effective_state(row)
        job = {"action": action, "id": row["id"]}
        if action == "register":
            if (
                row.get("device_id")
                or row["state"] not in ("draft", "failed")
                or row.get("post_uncertain")
            ):
                raise ClientError("此客户端已有或可能已有云端身份，请先刷新核对，不能重复注册")
            require_system_team(row["organization"])
            if payload["accept_terms"] is not True:
                raise ClientError("请先确认 Cloudflare 服务条款和独立注册操作")
            if service_form:
                credentials, remember = payload["service_auth"], payload["remember_service_auth"]
                if type(remember) is not bool or credentials is None and remember:
                    raise ClientError("保存服务授权设置无效")
                if credentials is None:
                    if not saved_service_auth(row["organization"]):
                        raise ClientError("本团队没有可用的已保存服务授权，请重新填写凭据")
                else:
                    credentials = provider.service_credentials(credentials)
                job.update(service_auth=credentials, remember_service_auth=remember)
            else:
                job["jwt"] = provider.team_token(payload["token"], row["organization"], time.time())
        else:
            if (
                not row.get("device_id")
                or action != "stop_subscription"
                and (not row.get("device_token") or row["state"] == "revoked")
            ):
                raise ClientError("没有可核验的本模块云端注册")
            if action == "revoke" and payload["confirm"] != row["name"]:
                raise ClientError("请填写客户端名称确认撤销")
        if action == "select_exit":
            job.update(
                vnet_id=provider.network_id(payload["vnet_id"]),
                expected_vnet_id=provider.network_id(payload["expected_vnet_id"]),
            )
        if (
            action in ("publish_subscription", "stop_subscription")
            and client_protocol(row) == "wireguard"
        ):
            raise ClientError("WireGuard 客户端请使用配置文件或二维码导入")
        if action == "publish_subscription" and (
            payload["confirm"] is not True or row["state"] != "registered" or not network_ready(row)
        ):
            raise ClientError("请先核验注册和指定出口，并确认将此手机配置保存为私密订阅")
        if action == "stop_subscription" and payload["confirm"] != row["name"]:
            raise ClientError("请填写客户端名称确认停止订阅")
        previous = row["state"]
        job["resume_state"] = previous
        operation_id = uuid.uuid4().hex
        directory(VOLATILE)
        job_path = VOLATILE / (operation_id + ".json")
        atomic(job_path, json.dumps(job))
        row.update(
            state="queued",
            operation=action,
            operation_id=operation_id,
            operation_at=time.time(),
            updated_at=time.time(),
        )
        row.pop("error", None)
        save(doc)
        try:
            spawn(row["id"], operation_id)
        except OSError:
            job_path.unlink(missing_ok=True)
            row.update(state=previous, error="任务未能启动；尚未提交云端操作")
            save(doc)
            raise ClientError(row["error"]) from None
        return dict(queued=True, operation_id=operation_id, client=public_row(row))


def preserve_unrecognized_reply(row, reply, login_jwt):
    # This is a private ownership receipt, never a log or a read-only RPC field.
    # Keep an unrecognized successful reply available for safe reconciliation.
    receipt = dict(
        owner=OWNER, client_id=row["id"], operation_id=row["operation_id"], response=reply
    )
    raw = json.dumps(receipt, ensure_ascii=False)
    if login_jwt:
        raw = raw.replace(login_jwt, "[authorization removed]")
    atomic(PRIVATE / ("pending-enrollment-" + row["id"] + ".json"), raw)


def verify_reply(doc, row, reply):
    protocol = client_protocol(row)
    cfg = provider.registered_config(
        reply, row["organization"], row["device_id"], row["public_key"], protocol=protocol
    )
    if any(
        r["id"] != row["id"]
        and r["organization"] == row["organization"]
        and (r.get("ipv4") == cfg["ipv4"] or cfg.get("ipv6") and r.get("ipv6") == cfg["ipv6"])
        and r["state"] != "revoked"
        for r in doc["clients"]
    ):
        raise ClientError("分配 IP 与另一客户端重复；未生成导出，请核对云端记录")
    if protocol == "wireguard" and not row.get("ipv4"):
        current = settings(row["settings"], protocol)
        defaults = settings({}, protocol)
        for key in ("endpoint", "port"):
            if current[key] == defaults[key]:
                current[key] = cfg["suggested_" + key]
        row["settings"] = current
    row.update(cfg)
    row.update(
        state="registered",
        last_checked_at=time.time(),
        updated_at=time.time(),
        post_uncertain=False,
    )
    row.pop("error", None)


def read_network(row, reply, target=None):
    protocol = {"protocol": "wireguard"} if client_protocol(row) == "wireguard" else {}
    return provider.client_networks(
        reply,
        row["organization"],
        row["device_id"],
        row["public_key"],
        row["device_token"],
        target=target,
        **protocol,
    )


def sync_network(row, reply):
    try:
        catalog = read_network(row, reply)
        old = row.get("network", {})
        row["network"] = dict(state="confirmed", catalog=catalog, checked_at=time.time())
        for key in ("requested_id", "previous_id", "applied_at"):
            if key in old:
                row["network"][key] = old[key]
    except ClientError as error:
        old = row.get("network", {})
        row["network"] = dict(old, state="error", error=str(error))


def apply_exit(doc, row, payload):
    row["state"] = "selecting_exit"
    save(doc)
    reply = provider.request("GET", "/reg/" + row["device_id"], token=row["device_token"])
    provider.registered_config(
        reply,
        row["organization"],
        row["device_id"],
        row["public_key"],
        protocol=client_protocol(row),
    )
    catalog = read_network(row, reply)
    target = provider.network_id(payload["vnet_id"])
    if not any(n["id"] == target and not n["is_default"] for n in catalog["networks"]):
        raise ClientError("所选出口不在此客户端的可选目录中；未应用，也未回落到默认网络")
    row["network"] = dict(state="confirmed", catalog=catalog, checked_at=time.time())
    if catalog["selected_id"] != payload["expected_vnet_id"]:
        raise ClientError("云端出口已发生变化；已读取当前值，请确认后重新选择")
    if catalog["selected_id"] != target:
        # Persist the previous selection before the only cloud write. A timeout
        # or killed worker must never be displayed as a confirmed new exit.
        row["network"].update(
            state="pending", requested_id=target, previous_id=catalog["selected_id"]
        )
        save(doc)
        acknowledged = read_network(row, reply, target)
        if acknowledged != {"acknowledged": True}:
            raise ClientError("云端尚未确认出口设置，请同步核对", uncertain=True)
        catalog = read_network(row, reply)
        if catalog["selected_id"] != target:
            raise ClientError("云端读回的出口与所选值不一致，请同步核对", uncertain=True)
    previous = row["network"].get("previous_id", catalog["selected_id"])
    verify_reply(doc, row, reply)
    row["network"] = dict(
        state="confirmed",
        catalog=catalog,
        checked_at=time.time(),
        requested_id=target,
        previous_id=previous,
        applied_at=time.time(),
    )


def job(ident, operation_id):
    if not re.fullmatch("[a-f0-9]{32}", operation_id):
        raise ClientError("任务标识无效")
    job_path = VOLATILE / (operation_id + ".json")
    with locked(wait=5):
        doc = load()
        row = find(doc, ident)
        if row.get("operation_id") != operation_id or row["state"] != "queued":
            raise ClientError("任务已失效")
        payload = read_private(job_path, 20000)
        job_path.unlink(missing_ok=True)  # One-time credentials are removed when claimed.
        action = payload["action"]
        if (
            payload["id"] != ident
            or action != row["operation"]
            or not -5 <= time.time() - row["operation_at"] <= 120
        ):
            payload.clear()
            row.update(
                state="failed" if action == "register" and not row.get("device_id") else "review",
                error="任务启动前已过期，本次云端操作未提交；可重新提交",
            )
            save(doc)
            return
        posting = False
        identity_checked = False
        try:
            if action == "register":
                require_system_team(row["organization"])
                protocol = client_protocol(row)
                credentials = payload.get("service_auth")
                if "service_auth" in payload:
                    if credentials is None:
                        try:
                            credentials = read_private(service_auth_path(row["organization"]), 2048)
                        except ClientError:
                            raise ClientError("已保存的服务授权无法读取，请重新填写凭据") from None
                    jwt = provider.service_auth_token(row["organization"], credentials, time.time())
                else:
                    jwt = provider.team_token(payload["jwt"], row["organization"], time.time())
                # Generate and persist the independent key before any cloud write.
                row.update(
                    provider.new_wireguard_keys()
                    if protocol == "wireguard"
                    else provider.new_keys()
                )
                row["fingerprint"] = hashlib.sha256(base64.b64decode(row["public_key"])).hexdigest()
                row["bootstrap_fingerprint"] = hashlib.sha256(
                    base64.b64decode(row["bootstrap_key"])
                ).hexdigest()
                row["bootstrap_serial"] = str(uuid.UUID(operation_id))
                row.update(state="registering", updated_at=time.time())
                save(doc)
                posting = True
                reply = provider.register(jwt, row["bootstrap_key"], row["bootstrap_serial"])
                try:
                    device_id, device_token = provider.response_identity(reply)
                except ClientError:
                    preserve_unrecognized_reply(row, reply, jwt)
                    raise
                if any(
                    r["id"] != row["id"]
                    and r.get("device_id")
                    and provider.registration_key(r["device_id"])
                    == provider.registration_key(device_id)
                    for r in doc["clients"]
                ):
                    raise ClientError(
                        "云端返回已有客户端身份，未登记新密钥；原有身份未改动", uncertain=True
                    )
                if protocol == "wireguard":
                    # Own the ID only after Cloudflare confirms the newly generated key.
                    try:
                        checked = provider.request("GET", "/reg/" + device_id, token=device_token)
                        provider.registered_config(
                            checked,
                            row["organization"],
                            device_id,
                            row["public_key"],
                            protocol=protocol,
                        )
                    except ClientError:
                        preserve_unrecognized_reply(row, reply, jwt)
                        raise ClientError(
                            "WireGuard 注册结果待核对，未认领云端身份，请勿重复注册", uncertain=True
                        ) from None
                    reply = checked
                row.update(
                    device_id=device_id,
                    device_token=device_token,
                    state="enrolling",
                    post_uncertain=False,
                )
                save(doc)  # Own the returned ID before enrollment or any possible cleanup.
                provider.check_team(reply, row["organization"])
                posting = False
                if protocol == "masque":
                    reply = provider.enroll(device_id, device_token, row["public_key"], row["name"])
                verify_reply(doc, row, reply)
                sync_network(row, reply)
                if payload.get("remember_service_auth"):
                    try:
                        atomic(service_auth_path(row["organization"]), json.dumps(credentials))
                    except OSError:
                        row["error"] = (
                            "此客户端已注册，但未能保存服务授权。下次新增设备时请重新填写凭据。"
                        )
            elif action == "refresh":
                row["state"] = "refreshing"
                save(doc)
                reply = provider.request(
                    "GET", "/reg/" + row["device_id"], token=row["device_token"]
                )
                verify_reply(doc, row, reply)
                sync_network(row, reply)
                if (
                    row.get("subscription", {}).get("state") == "published"
                    and network_ready(row)
                    and subscriptions.state(row, portable_uri(row))["needs_update"]
                ):
                    try:
                        subscriptions.publish(
                            PRIVATE, read_private, row, portable_uri(row), lambda: save(doc)
                        )
                    except ClientError as error:
                        row["subscription"].update(state="unknown", error=str(error))
            elif action == "select_exit":
                apply_exit(doc, row, payload)
            elif action == "publish_subscription":
                reply = provider.request(
                    "GET", "/reg/" + row["device_id"], token=row["device_token"]
                )
                verify_reply(doc, row, reply)
                identity_checked = True
                sync_network(row, reply)
                if not network_ready(row):
                    raise ClientError("云端指定出口尚未核验，未发布新的订阅内容")
                row["state"] = "publishing_subscription"
                save(doc)
                subscriptions.publish(
                    PRIVATE, read_private, row, portable_uri(row), lambda: save(doc)
                )
                row.update(state="registered", updated_at=time.time())
                row.pop("error", None)
            elif action == "stop_subscription":
                row["state"] = "stopping_subscription"
                save(doc)
                subscriptions.stop(PRIVATE, read_private, row, lambda: save(doc))
                row.update(state=payload["resume_state"], updated_at=time.time())
                row.pop("error", None)
            elif action == "revoke":
                row["state"] = "revoking"
                save(doc)
                acknowledged = provider.request(
                    "DELETE", "/reg/" + row["device_id"], token=row["device_token"]
                )
                if acknowledged != {"deleted": True}:
                    raise ClientError("云端未确认撤销，保留本地凭据", uncertain=True)
                row.update(state="revoked", updated_at=time.time(), post_uncertain=False)
                for k in ("private_key", "device_token", "bootstrap_key", "peer_key", "reserved"):
                    row.pop(k, None)
            else:
                raise ClientError("未知客户端任务")
        except ClientError as error:
            if action == "select_exit" and row.get("network", {}).get("state") == "pending":
                row["network"].update(state="unknown", error=str(error))
            uncertain = posting and error.uncertain
            state = (
                "unknown"
                if uncertain or action == "revoke" and error.uncertain
                else "review"
                if row.get("device_id")
                else "failed"
            )
            if action in ("publish_subscription", "stop_subscription"):
                state = (
                    payload["resume_state"]
                    if action == "stop_subscription" or identity_checked
                    else "review"
                )
                if row.get("subscription"):
                    row["subscription"].update(
                        state="prepared"
                        if row["subscription"]["state"] == "prepared"
                        else "unknown",
                        error=str(error),
                    )
            row.update(state=state, error=str(error), updated_at=time.time())
            if uncertain:
                row["post_uncertain"] = True
        except Exception:
            if action in ("publish_subscription", "stop_subscription") and row.get("subscription"):
                row["subscription"].update(
                    state="prepared" if row["subscription"]["state"] == "prepared" else "unknown",
                    error="订阅任务中断，请核对后再次同步；原链接保留",
                )
            row.update(
                state="unknown",
                error="任务中断，结果未确认；请刷新核对，勿重复注册",
                updated_at=time.time(),
                post_uncertain=posting or row.get("post_uncertain", False),
            )
        finally:
            payload.clear()
            row.pop("bootstrap_key", None)
            save(doc)


def edit(payload):
    if set(payload) != {"id", "settings"}:
        raise ClientError("连接参数输入无效")
    with locked():
        doc = load()
        row = find(doc, payload["id"])
        value = settings(payload["settings"], client_protocol(row))
        if active(row):
            raise ClientError("客户端任务进行中，暂不能修改导出参数")
        row.update(settings=value, updated_at=time.time())
        save(doc)
    return dict(client=public_row(row))


def portable_uri(row):
    cfg = settings(row["settings"])
    query = {
        "publicKey": row["peer_key"],
        "privateKey": row["private_key"],
        "ip": row["ipv4"],
        "dns": cfg["dns"],
        "mtu": cfg["mtu"],
        "sni": "zt-masque.cloudflareclient.com",
        "udp": 1,
        "cc": cfg["cc"],
        "keepalive": cfg["keepalive"],
    }
    return (
        "masque://"
        + cfg["endpoint"]
        + ":"
        + str(cfg["port"])
        + "?"
        + urllib.parse.urlencode(query)
        + "#"
        + urllib.parse.quote(row["name"], safe="")
    )


def portable_wireguard(row):
    cfg = settings(row["settings"], "wireguard")
    addresses = [row["ipv4"] + "/32"]
    allowed = ["0.0.0.0/0"]
    if row.get("ipv6"):
        addresses.append(row["ipv6"] + "/128")
        allowed.append("::/0")
    return "\n".join(
        [
            "[Interface]",
            "PrivateKey = " + row["private_key"],
            "Address = " + ", ".join(addresses),
            "DNS = " + cfg["dns"],
            "MTU = " + str(cfg["mtu"]),
            "",
            "[Peer]",
            "PublicKey = " + row["peer_key"],
            *("AllowedIPs = " + ip for ip in allowed),
            "Endpoint = %s:%s" % (cfg["endpoint"], cfg["port"]),
            "PersistentKeepalive = " + str(cfg["keepalive"]),
            "Reserved = " + ",".join(map(str, row["reserved"])),
        ]
    )


def export(payload):
    if set(payload) != {"id", "confirm"} or payload["confirm"] is not True:
        raise ClientError("请确认此导出包含客户端私钥")
    with locked():
        doc = load()
        row = find(doc, payload["id"])
        if row["state"] != "registered" or not row.get("private_key") or not row.get("peer_key"):
            raise ClientError("注册尚未核验，不能导出")
        if not network_ready(row):
            raise ClientError("请先同步并选择此客户端的出口；默认或未核对的网络不能导出")
        wireguard = client_protocol(row) == "wireguard"
        uri = portable_wireguard(row) if wireguard else portable_uri(row)
        filename = "imok-client-" + row["id"][:8] + (".conf" if wireguard else ".txt")
        row["last_exported_at"] = time.time()
        save(doc)
        return dict(
            uri=uri,
            filename=filename,
            exit_verified=False,
            network_selection_verified=True,
            selected_network=public_network(row)["selected_name"],
            message="已核对云端出口选择；手机实际连接与公网出口仍需重连验证。此配置包含私钥。",
        )


def subscription_link(payload):
    if set(payload) != {"id", "confirm"} or payload["confirm"] is not True:
        raise ClientError("请确认此私密订阅链接具有读取手机私钥的权限")
    with locked():
        row = find(load(), payload["id"])
        if client_protocol(row) == "wireguard":
            raise ClientError("WireGuard 客户端请使用配置文件或二维码导入")
        status = subscriptions.state(
            row, portable_uri(row) if row.get("private_key") and row.get("peer_key") else None
        )
        if (
            row["state"] != "registered"
            or not network_ready(row)
            or status["state"] != "published"
            or status["needs_update"]
        ):
            raise ClientError("订阅尚未同步并确认，不能交付链接；请先更新私密订阅")
        return dict(
            uri=subscriptions.private_url(PRIVATE, read_private, row),
            filename="imok-subscription.txt",
        )


def delete(payload):
    if set(payload) != {"id", "confirm"}:
        raise ClientError("删除输入无效")
    with locked():
        doc = load()
        row = find(doc, payload["id"])
        if payload["confirm"] != row["name"]:
            raise ClientError("请填写客户端名称确认删除")
        if (
            active(row)
            or row.get("post_uncertain")
            or row.get("device_id")
            and row["state"] != "revoked"
            or row["state"] in BUSY + ("unknown",)
        ):
            raise ClientError("云端身份尚未确认撤销，不能删除归属记录")
        if subscriptions.state(row)["enabled"]:
            raise ClientError("请先停止此客户端的私密订阅，确认云端内容已移除后再删除记录")
        (PRIVATE / ("pending-enrollment-" + row["id"] + ".json")).unlink(missing_ok=True)
        doc["clients"].remove(row)
        save(doc)
    return dict(deleted=True)


def prepare():
    # Client access must also work while the main proxy service is stopped.
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepare_requests(RUN)
    return {"ready": True}


def dispatch(action, request_id):
    if action not in (
        "create",
        "register",
        "refresh",
        "select_exit",
        "publish_subscription",
        "stop_subscription",
        "subscription_link",
        "revoke",
        "edit",
        "export",
        "delete",
        "forget_service_auth",
    ):
        raise ClientError("客户端操作无效")
    reap_requests(RUN)
    with claim_request(RUN / "client-input", request_id, 20000) as payload:
        if not isinstance(payload, dict):
            raise ClientError("客户端输入无效")
        if action in (
            "register",
            "refresh",
            "select_exit",
            "publish_subscription",
            "stop_subscription",
            "revoke",
        ):
            result = start(action, payload)
        else:
            result = {
                "create": create,
                "edit": edit,
                "export": export,
                "subscription_link": subscription_link,
                "delete": delete,
                "forget_service_auth": forget_service_auth,
            }[action](payload)
        return dict(result, request_id=request_id.split(":")[0])


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "job":
        raise SystemExit(2)
    try:
        job(sys.argv[2], sys.argv[3])
    except Exception:
        raise SystemExit(1) from None
