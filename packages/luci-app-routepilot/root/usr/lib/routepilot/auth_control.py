"""Guard first-time registration; never overwrite an existing router identity."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
import time
from public_errors import ControlError
from runtime import RUN, atomic, read_config
from mplane import ROOT, cli


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
            raise ControlError("路由器已有注册身份，无需重复注册；请读取账户出口，原身份未改动")
        path = RUN / "auth-pending.json"
        # Unknown interrupted attempts cannot be mistaken for owned registration.
        atomic(path, json.dumps(dict(phase="starting", time=time.time(), organization=org)))
        cli("registration", "new", org)
        atomic(
            path,
            json.dumps(
                dict(phase="ready", time=time.time(), organization=org, identity=identity())
            ),
        )
        return "https://" + org + ".cloudflareaccess.com/warp"


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
        ):
            raise ControlError("登录输入无效")
        token = payload["token"].strip()
        if not token.startswith(
            "com.cloudflare.warp://" + org + ".cloudflareaccess.com/auth?token="
        ):
            raise ControlError("登录链接格式不正确")
        cli("registration", "token", token)
        (RUN / "auth-pending.json").unlink(missing_ok=True)
        return dict(
            registered=True,
            request_id=request_id.split(":")[0],
            message="路由器已注册；请读取账户出口并明确选择，再启用设备",
        )
