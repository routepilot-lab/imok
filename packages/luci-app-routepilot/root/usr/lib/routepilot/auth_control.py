"""Guard first-time registration; never overwrite an existing router identity."""

from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import re
import subprocess
import time
from public_errors import ControlError
from runtime import RUN, atomic, read_config
from mplane import ROOT, cli

AUTH_LINK = re.compile(r"""(?<![\w.+-])com\.cloudflare\.warp\\?://[^\s"'<>\x60]+""")
JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def organization(value):
    if not isinstance(value, str):
        raise ControlError("请填写有效团队名称，不填网址或邮箱")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ControlError("请填写有效团队名称，不填网址或邮箱")
    return value


@contextmanager
def guard():
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "auth.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("另一个登录操作正在进行，请稍后再试") from None
        yield


def identity():
    root = ROOT / "var/lib/cloudflare-warp"
    result = {}
    for name in ["reg.json", "reg_mdm_orgs.json"]:
        p = root / name
        if p.is_symlink():
            raise ControlError("注册文件由其他入口管理；未改动身份")
        if p.exists():
            result[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def context():
    cfg = read_config()["main"]
    if cfg.get("enabled") == "1":
        raise ControlError("主线路正在使用，不能重新注册；现有身份未改动")
    state = json.loads((RUN / "status.json").read_text())
    if state.get("state") != "preparing" or not 0 <= time.time() - state.get("time", 0) < 15:
        raise ControlError("登录环境尚未就绪，请先准备登录环境")
    return organization(cfg.get("organization", ""))


def pending(org):
    p = RUN / "auth-pending.json"
    try:
        if p.is_symlink() or p.stat().st_mode & 0o077:
            raise ValueError()
        row = json.loads(p.read_text())
        if (
            row.get("organization") != org
            or row.get("phase") != "ready"
            or not 0 <= time.time() - row.get("time", 0) < 1800
            or row.get("identity") != identity()
        ):
            raise ValueError()
        return row
    except (OSError, ValueError):
        return None


def login():
    with guard():
        org = context()
        if pending(org):
            return "https://" + org + ".cloudflareaccess.com/warp"
        if identity():
            marker = RUN / "auth-pending.json"
            try:
                row = json.loads(marker.read_text())
            except (OSError, ValueError):
                row = None
            if isinstance(row, dict) and row.get("phase") == "starting":
                raise ControlError(
                    "上次注册初始化中断，无法确认本机注册资料是否完整。"
                    "请先检查账户是否能读取出口，再核对 Cloudflare 设备记录；原资料已保留"
                )
            raise ControlError("路由器已有注册身份，无需重复注册；请读取账户出口，原身份未改动")
        path = RUN / "auth-pending.json"
        # Unknown interrupted attempts cannot be mistaken for owned registration.
        atomic(path, json.dumps(dict(phase="starting", time=time.time(), organization=org)))
        try:
            cli("registration", "new", org)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            created = identity()
            if not created:
                path.unlink(missing_ok=True)
                raise ControlError("WARP 未完成注册初始化，请检查登录环境后重试") from None
            if not isinstance(error, subprocess.TimeoutExpired):
                raise ControlError(
                    "WARP 初始化未完成，已保留本机注册资料。请检查账户出口和 Cloudflare 设备记录"
                ) from None
            # This request started from an empty identity and still owns the auth lock.
            # Let the official token operation finish the same attempt if supported.
            # No new request may replace these files after the attempt expires.
            atomic(
                path,
                json.dumps(dict(phase="ready", time=time.time(), organization=org, identity=created)),
            )
            return "https://" + org + ".cloudflareaccess.com/warp"
        atomic(
            path,
            json.dumps(
                dict(phase="ready", time=time.time(), organization=org, identity=identity())
            ),
        )
        return "https://" + org + ".cloudflareaccess.com/warp"


def registration_link(value, org):
    prefix = "com.cloudflare.warp://" + org + ".cloudflareaccess.com/auth?token="
    links = {link.replace(r"\://", "://", 1) for link in AUTH_LINK.findall(value)}
    if links:
        if len(links) != 1:
            raise ControlError("粘贴内容包含多条授权链接，请只复制本次登录成功页的一行")
        link = links.pop()
        if not link.startswith(prefix):
            raise ControlError("授权链接与当前团队不一致，请从 imOK 重新打开登录页")
        token = link[len(prefix) :]
        # Keep complete links with opaque tokens compatible with the official client.
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise ControlError("授权链接格式不正确，请复制成功页中含 token= 的整行")
    else:
        token = value.strip().removeprefix("token=").rstrip("'\x22;")
        if not JWT.fullmatch(token):
            raise ControlError("请粘贴 token= 后的完整令牌，或成功页中含 token= 的整行")

    if JWT.fullmatch(token):
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
            expiry = claims["exp"]
            issuer = claims["iss"]
            if type(expiry) not in (int, float):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ControlError("授权令牌内容无效，请重新登录后复制") from None
        if issuer not in (
            "https://" + org + ".cloudflareaccess.com",
            "https://" + org + ".cloudflareaccess.com/",
        ):
            raise ControlError("授权令牌与当前团队不一致，请从 imOK 重新打开登录页")
        if not time.time() < expiry < float("inf"):
            raise ControlError("授权令牌已过期，请重新登录并立即提交")
    # Cloudflare verifies the signature when the official client submits the token.
    return prefix + token


def register(request_id=None):
    path = RUN / "auth-input"
    from request_input import claim_request

    with guard(), claim_request(path, request_id, 16000) as payload:
        org = context()
        if not pending(org):
            raise ControlError(
                "未找到当前有效的首次登录请求；已有身份不会被覆盖，请重新读取账户状态"
            )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"token"}
            or not isinstance(payload["token"], str)
            or len(payload["token"]) > 15000
        ):
            raise ControlError("登录输入无效")
        cli("registration", "token", registration_link(payload["token"], org))
        (RUN / "auth-pending.json").unlink(missing_ok=True)
        return dict(
            registered=True,
            request_id=request_id.split(":")[0],
            message="路由器已注册；请读取账户出口并明确选择，再启用设备",
        )
