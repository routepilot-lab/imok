"""Private fixed subscriptions isolated from every proxy and backup Worker.

Only an explicitly selected portable profile is stored as a Worker secret.
No router keys, JWTs, full backups, public write API or router ports are used.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import client_provider as provider
from client_provider import ClientError
from runtime import atomic

CLOUD_AUTH = Path("/etc/routepilot/backup/cloud.json")
BUNDLE = Path("/usr/share/routepilot/client-subscription/worker.mjs")
API = "https://api.cloudflare.com/client/v4"


def token():
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def binding(value):
    if not isinstance(value, str) or not re.fullmatch("[A-Za-z0-9_-]{43}", value):
        raise ClientError("私密订阅凭据无效")
    return "S_" + hashlib.sha256(value.encode()).hexdigest()


def body(uri):
    if (
        not isinstance(uri, str)
        or not uri.startswith("masque://")
        or len(uri) > 6000
        or "\n" in uri
        or "\r" in uri
    ):
        raise ClientError("订阅连接内容无效")
    encoded = base64.b64encode((uri + "\n").encode()).decode()
    return dict(schema=1, revision=hashlib.sha256(encoded.encode()).hexdigest(), body=encoded)


def state(row, uri=None):
    value = row.get("subscription")
    if value is None:
        return dict(state="none", enabled=False, needs_update=False, synced_at=0)
    if (
        not isinstance(value, dict)
        or set(value) - {"token", "state", "revision", "synced_at", "error"}
        or value.get("state")
        not in ("prepared", "publishing", "published", "unknown", "stopping", "disabled")
    ):
        raise ClientError("私密订阅记录无效，未重置链接")
    binding(value.get("token"))
    revision = value.get("revision", "")
    if revision and (not isinstance(revision, str) or not re.fullmatch("[a-f0-9]{64}", revision)):
        raise ClientError("订阅版本记录无效")
    stamp = value.get("synced_at", 0)
    if type(stamp) not in (int, float) or not 0 <= stamp < 10**12:
        raise ClientError("订阅更新时间无效")
    result = dict(
        state=value["state"],
        enabled=value["state"] != "disabled",
        synced_at=stamp,
        needs_update=bool(uri is not None and revision != body(uri)["revision"]),
    )
    if value.get("error"):
        if not isinstance(value["error"], str) or len(value["error"]) > 300:
            raise ClientError("订阅错误记录无效")
        result["error"] = value["error"]
    return result


def authorization(read_private):
    try:
        data = read_private(CLOUD_AUTH, 8192)
    except ClientError:
        raise ClientError(
            "请先在 Workers 页面保存受限 Cloudflare API 授权；订阅不会修改备用线路"
        ) from None
    account, credential = data.get("account_id"), data.get("token")
    if (
        not isinstance(account, str)
        or not re.fullmatch("[a-f0-9]{32}", account)
        or not isinstance(credential, str)
        or not re.fullmatch("[A-Za-z0-9_-]{20,256}", credential)
    ):
        raise ClientError("Cloudflare 账户或 API 授权无效，请更新 Workers 页面中的授权")
    zone = data.get("zone_id", "")
    if zone and (not isinstance(zone, str) or not re.fullmatch("[a-f0-9]{32}", zone)):
        raise ClientError("订阅域名授权区域无效")
    return dict(account=account, token=credential, zone_id=zone)


def api(auth, method, suffix, payload=None, module=None, missing=False):
    worker_path = re.fullmatch(
        r"/workers/(?:subdomain|scripts/imok-clients-[a-f0-9]{12}(?:/(?:settings|subdomain|secrets(?:/S_[a-f0-9]{64})?))?|domains(?:\?hostname=imok-clients-[a-f0-9]{12}(?:\.[a-z0-9-]{1,63}){1,8})?)",
        suffix,
    )
    zone_path = re.fullmatch(
        r"/zones/([a-f0-9]{32})(?:/dns_records\?name=imok-clients-[a-f0-9]{12}(?:\.[a-z0-9-]{1,63}){1,8}&per_page=100)?",
        suffix,
    )
    if not worker_path and not (
        zone_path and method == "GET" and zone_path[1] == auth.get("zone_id")
    ):
        raise ClientError("订阅管理路径无效")
    path = "/accounts/" + auth["account"] + suffix if worker_path else suffix
    allowed = (
        {"GET"}
        if zone_path
        or suffix == "/workers/subdomain"
        or "/domains?" in suffix
        or suffix.endswith("/settings")
        else {"PUT"}
        if suffix == "/workers/domains"
        else {"GET", "POST"}
        if suffix.endswith("/subdomain")
        else {"GET", "PUT"}
        if suffix.endswith("/secrets")
        else {"DELETE"}
        if "/secrets/" in suffix
        else {"PUT"}
    )
    if method not in allowed:
        raise ClientError("订阅操作无效")
    headers = {"Authorization": "Bearer " + auth["token"], "Accept": "application/json"}
    data = None
    if module is not None:
        boundary = "imok-" + uuid.uuid4().hex
        data = (
            (
                "--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n'
                + json.dumps(payload)
                + "\r\n--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="subscription.mjs"; filename="subscription.mjs"\r\nContent-Type: application/javascript+module\r\n\r\n'
            ).encode()
            + module
            + ("\r\n--" + boundary + "--\r\n").encode()
        )
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, method=method, headers=headers, data=data)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        provider.NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(524289)
            if not 200 <= response.status < 300 or len(raw) > 524288:
                raise ValueError()
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or result.get("errors")
            ):
                raise ValueError()
            return result.get("result")
    except urllib.error.HTTPError as error:
        if missing and method == "GET" and error.code == 404:
            return None
        messages = {
            401: "Cloudflare API 授权失效，请在 Workers 页面更新",
            403: "API 授权缺少订阅 Worker 的脚本权限",
            429: "Cloudflare API 请求受限，请稍后同步订阅",
        }
        raise ClientError(
            messages.get(error.code, "订阅服务请求失败（HTTP %s）" % error.code),
            uncertain=method != "GET",
        ) from None
    except ClientError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise ClientError(
            "订阅云端结果未确认，保留原私密链接，请稍后核对", uncertain=method != "GET"
        ) from None


def publisher(private, read_private, auth, create=False):
    path = private / "subscription-publisher.json"
    if not path.exists():
        if not create:
            raise ClientError("尚未创建私密订阅服务")
        value = dict(
            schema=1,
            account=auth["account"],
            owner=uuid.uuid4().hex,
            script="imok-clients-" + uuid.uuid4().hex[:12],
        )
        atomic(path, json.dumps(value))
    value = read_private(path, 4096)
    if (
        set(value)
        - {"schema", "account", "owner", "script", "host", "deployed", "domain_id", "zone_id"}
        or value.get("schema") != 1
        or value.get("account") != auth["account"]
        or not re.fullmatch("[a-f0-9]{32}", str(value.get("owner", "")))
        or not re.fullmatch("imok-clients-[a-f0-9]{12}", str(value.get("script", "")))
    ):
        raise ClientError("订阅服务归属或账户已变化，未改动已有 Worker")
    prefix = "/workers/scripts/" + value["script"]
    settings = api(auth, "GET", prefix + "/settings", missing=True)
    if settings is not None:
        if (
            not isinstance(settings, dict)
            or not isinstance(settings.get("bindings"), list)
            or not all(isinstance(b, dict) for b in settings["bindings"])
        ):
            raise ClientError("订阅 Worker 归属响应无效，拒绝覆盖")
        owners = [
            b.get("text")
            for b in settings.get("bindings", [])
            if b.get("name") == "IMOK_CLIENT_OWNER" and b.get("type") == "plain_text"
        ]
        if owners != [value["owner"]]:
            raise ClientError("订阅 Worker 归属不匹配，拒绝覆盖")
    elif not create or value.get("deployed"):
        raise ClientError("已记录的订阅 Worker 不存在，未自动新建或替换")
    else:
        source = BUNDLE.read_bytes()
        metadata = dict(
            main_module="subscription.mjs",
            compatibility_date="2026-10-01",
            bindings=[dict(name="IMOK_CLIENT_OWNER", type="plain_text", text=value["owner"])],
            observability=dict(enabled=False),
            logpush=False,
        )
        api(auth, "PUT", prefix, metadata, module=source)
    if create and not value.get("deployed"):
        api(auth, "POST", prefix + "/subdomain", dict(enabled=True, previews_enabled=False))
        value["deployed"] = True
        atomic(path, json.dumps(value))
    # Preserve the address of every already published client. A workers.dev
    # service may move to an owned domain only before a real link was issued.
    registered = (
        read_private(private / "registry.json")
        if (private / "registry.json").exists()
        else {"clients": []}
    )
    issued = any(r.get("subscription", {}).get("revision") for r in registered["clients"])
    if value.get("domain_id"):
        host = value.get("host", "")
        validate_host(host)
        bindings = api(auth, "GET", "/workers/domains?hostname=" + host)
        if not isinstance(bindings, list) or not any(
            isinstance(b, dict)
            and b.get("id") == value["domain_id"]
            and b.get("hostname") == host
            and b.get("service") == value["script"]
            for b in bindings
        ):
            raise ClientError("订阅域名归属已变化，未改写或更换地址")
    elif auth.get("zone_id") and not issued:
        zone = api(auth, "GET", "/zones/" + auth["zone_id"])
        if (
            not isinstance(zone, dict)
            or zone.get("account", {}).get("id") != auth["account"]
            or zone.get("status") != "active"
        ):
            raise ClientError("订阅域名不属于当前授权账户或区域未生效")
        host = value["script"] + "." + str(zone.get("name", ""))
        validate_host(host)
        bindings = api(auth, "GET", "/workers/domains?hostname=" + host)
        if not isinstance(bindings, list) or not all(isinstance(b, dict) for b in bindings):
            raise ClientError("订阅域名目录无效")
        bound = next((b for b in bindings if b.get("hostname") == host), None)
        if bound and (
            bound.get("service") != value["script"] or bound.get("zone_id") != auth["zone_id"]
        ):
            raise ClientError("订阅子域名已被其他项目使用，拒绝覆盖")
        records = api(
            auth, "GET", "/zones/" + auth["zone_id"] + "/dns_records?name=" + host + "&per_page=100"
        )
        if not isinstance(records, list) or records and not bound:
            raise ClientError("订阅子域名已有 DNS 记录，未覆盖；请核对归属")
        if not bound:
            bound = api(
                auth,
                "PUT",
                "/workers/domains",
                dict(hostname=host, service=value["script"], zone_id=auth["zone_id"], enabled=True),
            )
        if (
            not isinstance(bound, dict)
            or bound.get("hostname") != host
            or bound.get("service") != value["script"]
            or not isinstance(bound.get("id"), str)
        ):
            raise ClientError("订阅域名绑定尚未确认，保留已有服务待核对", uncertain=True)
        value.update(host=host, domain_id=bound["id"], zone_id=auth["zone_id"])
    else:
        subdomain = api(auth, "GET", "/workers/subdomain")
        sub = subdomain.get("subdomain") if isinstance(subdomain, dict) else None
        if not isinstance(sub, str) or not re.fullmatch(
            "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", sub
        ):
            raise ClientError("账户尚无可用的 workers.dev 子域名，未开放路由器端口")
        route = api(auth, "GET", prefix + "/subdomain")
        if not isinstance(route, dict) or route.get("enabled") is not True:
            raise ClientError("私密订阅的 HTTPS 地址尚未启用，请核对订阅服务")
        host = value["script"] + "." + sub + ".workers.dev"
        if issued and value.get("host") and value["host"] != host:
            raise ClientError("账户子域名已变化，原订阅地址未替换")
        value["host"] = host
    value["deployed"] = True
    atomic(path, json.dumps(value))
    return value


def validate_host(host):
    if (
        not isinstance(host, str)
        or len(host) > 253
        or not re.fullmatch(
            r"imok-clients-[a-f0-9]{12}(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?){2,8}", host
        )
    ):
        raise ClientError("订阅地址无效")
    return host


def private_url(private, read_private, row):
    value = read_private(private / "subscription-publisher.json", 4096)
    host = value.get("host", "")
    validate_host(host)
    sub = row.get("subscription", {})
    binding(sub.get("token"))
    return "https://" + host + "/s/" + sub["token"]


def fetch_body(url, expected=None):
    parsed = urllib.parse.urlsplit(url)
    validate_host(parsed.hostname)
    if (
        parsed.scheme != "https"
        or parsed.netloc != parsed.hostname
        or parsed.query
        or parsed.fragment
        or not re.fullmatch("/s/[A-Za-z0-9_-]{43}", parsed.path)
    ):
        raise ClientError("订阅校验地址无效")
    # Match the router's existing IPv4 management requests without changing
    # system IPv6, DNS, proxy settings or any active tunnel. URL stays off argv.
    try:
        with tempfile.TemporaryDirectory(prefix="imok-subscription-") as directory:
            content, headers = Path(directory) / "body", Path(directory) / "headers"
            for path in (content, headers):
                path.write_bytes(b"")
                path.chmod(0o600)
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
                "4",
                "--max-time",
                "12",
                "--max-filesize",
                "12000",
                "--max-redirs",
                "0",
                "--config",
                "-",
                "-H",
                "Cache-Control: no-cache",
                "-o",
                str(content),
                "-D",
                str(headers),
                "-w",
                "%{http_code}",
            ]
            result = subprocess.run(
                args,
                input='url = "' + url + '"\n',
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            code = result.stdout.strip()
            if result.returncode == 0 and expected is None and code == "404":
                return True
            raw = content.read_bytes()
            cache = headers.read_text(errors="replace")
            if (
                result.returncode
                or code != "200"
                or expected is None
                or len(raw) > 12000
                or raw.strip() != expected.encode()
                or not re.search(r"(?im)^cache-control:[^\r\n]*\bno-store\b", cache)
            ):
                raise ValueError()
            return True
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ClientError("订阅 IPv4 HTTPS 内容尚未验证，未宣称更新完成", uncertain=True) from None


def confirm_body(url, expected=None):
    # A Secret deployment or a network response can be briefly unavailable.
    # Retry reads only; never create another identity, token or deployment.
    for attempt in range(3):
        try:
            return fetch_body(url, expected)
        except ClientError:
            if attempt == 2:
                raise
            time.sleep(1)


def publish(private, read_private, row, uri, persist):
    content = body(uri)
    if "subscription" not in row or row["subscription"]["state"] == "disabled":
        row["subscription"] = dict(token=token(), state="prepared")
        persist()
    state(row, uri)
    if (
        row["subscription"].get("revision")
        and not (private / "subscription-publisher.json").exists()
    ):
        raise ClientError("原订阅服务归属记录丢失，未生成替代地址；请恢复私有备份")
    auth = authorization(read_private)
    service = publisher(private, read_private, auth, create=True)
    sub = row["subscription"]
    if sub["state"] in ("unknown", "publishing"):
        try:
            fetch_body(private_url(private, read_private, row), content["body"])
        except ClientError:
            pass
        else:
            sub.update(state="published", revision=content["revision"], synced_at=time.time())
            sub.pop("error", None)
            persist()
            return
    sub.update(state="publishing")
    sub.pop("error", None)
    persist()
    result = api(
        auth,
        "PUT",
        "/workers/scripts/" + service["script"] + "/secrets",
        dict(name=binding(sub["token"]), type="secret_text", text=json.dumps(content)),
    )
    if not isinstance(result, dict) or result.get("name") != binding(sub["token"]):
        raise ClientError("订阅私密存储未确认，保留原链接待核对", uncertain=True)
    confirm_body(private_url(private, read_private, row), content["body"])
    sub.update(state="published", revision=content["revision"], synced_at=time.time())
    sub.pop("error", None)
    persist()


def stop(private, read_private, row, persist):
    if not row.get("subscription") or row["subscription"]["state"] == "disabled":
        raise ClientError("此客户端没有启用中的私密订阅")
    sub = row["subscription"]
    if sub["state"] == "prepared" and not sub.get("revision"):
        # No Secret upload can have started before the persisted publishing state.
        sub.update(state="disabled", synced_at=time.time())
        sub.pop("error", None)
        persist()
        return
    auth = authorization(read_private)
    service = publisher(private, read_private, auth)
    sub["state"] = "stopping"
    persist()
    prefix = "/workers/scripts/" + service["script"] + "/secrets"
    listed = api(auth, "GET", prefix)
    if not isinstance(listed, list) or not all(isinstance(s, dict) for s in listed):
        raise ClientError("订阅私密存储列表未能核对", uncertain=True)
    if any(s.get("name") == binding(sub["token"]) for s in listed):
        api(auth, "DELETE", prefix + "/" + binding(sub["token"]))
    remaining = api(auth, "GET", prefix)
    if not isinstance(remaining, list) or any(
        not isinstance(s, dict) or s.get("name") == binding(sub["token"]) for s in remaining
    ):
        raise ClientError("云端订阅内容尚未确认移除，保留本地记录", uncertain=True)
    confirm_body(private_url(private, read_private, row))
    sub.update(state="disabled", synced_at=time.time())
    sub.pop("error", None)
    persist()
