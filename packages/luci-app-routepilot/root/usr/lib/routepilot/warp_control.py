"""Public WARP capabilities, explicit protocol preferences, and read-only egress.

Never exposes merged settings, device tokens, profile IDs or identity details.
Managed profiles remain under organization control; we never edit shared policy.
"""

import json
import hashlib
from pathlib import Path
import re
import time
from runtime import RUN, atomic, read_config
from public_errors import ControlError
from warp_cli import PROTOCOLS, UNKNOWN_WARNING, parse_settings as parse_client_settings

STATE = RUN / "warp-state.json"
OBSERVATION = RUN / "exit-observation.json"
ORGANIZATION_INPUT = RUN / "organization-input"


class UnrecognizedProtocol(ControlError):
    pass


class ProtocolMismatch(ControlError):
    pass


def parse_settings(text):
    state = parse_client_settings(text)
    if state is None:
        raise UnrecognizedProtocol("未能识别客户端有效协议，未修改协议或出口")
    return state


def preference(value):
    if value not in ("auto", *PROTOCOLS):
        raise ControlError("主线路协议仅支持跟随当前设置、MASQUE 或 WireGuard")
    return value


def configure_protocol(root, desired, organization=""):
    """Use documented per-client MDM, never overwrite another manager's file.

    Two allowed digests make the journal-before-file update crash recoverable.
    Default auto creates no file and does not write any existing configuration.
    """
    from auth_control import organization as normalize_organization

    organization = normalize_organization(organization) if organization else ""
    desired = preference(desired)
    folder = Path(root) / "var/lib/cloudflare-warp"
    target = folder / "mdm.xml"
    owner = folder / "routepilot-protocol-owner.json"
    if target.is_symlink() or owner.is_symlink():
        raise ControlError("客户端管理配置被其他入口占用；未覆盖")
    record = json.loads(owner.read_text()) if owner.exists() else {}
    if target.exists():
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest not in record.get("hashes", []):
            if desired == "auto":
                return  # Existing external MDM remains authoritative.
            raise ControlError("已有其他管理配置，不能覆盖；请选择跟随当前设置")
    if desired == "auto":
        if record:
            target.unlink(missing_ok=True)
            owner.unlink(missing_ok=True)
        return
    if organization and not re.fullmatch("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", organization):
        raise ControlError("组织名称格式无效，未生成协议配置")
    text = "<dict>\n" + (
        "<key>organization</key><string>" + organization + "</string>\n" if organization else ""
    )
    text += "<key>warp_tunnel_protocol</key><string>" + desired.lower() + "</string>\n</dict>\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return
    hashes = list(dict.fromkeys(record.get("hashes", []) + [digest]))[-2:]
    atomic(owner, json.dumps(dict(hashes=hashes)))
    atomic(target, text)


def ensure_protocol(desired, cli):
    desired = preference(desired)
    state = parse_settings(cli("settings").stdout)
    if desired != "auto" and desired != state["protocol"]:
        raise ProtocolMismatch(
            "客户端尚未采用本路由器所选的 "
            + desired
            + "；当前有效 "
            + state["protocol"]
            + "，未应用新出口或设备分流"
        )
    return state


def refresh():
    from mplane import cli, ROOT

    try:
        value = parse_settings(cli("settings").stdout)
        if value["source"] == "mdm":
            folder = ROOT / "var/lib/cloudflare-warp"
            target = folder / "mdm.xml"
            owner = folder / "routepilot-protocol-owner.json"
            try:
                owned = (
                    not target.is_symlink()
                    and not owner.is_symlink()
                    and hashlib.sha256(target.read_bytes()).hexdigest()
                    in json.loads(owner.read_text()).get("hashes", [])
                )
            except (OSError, ValueError, AttributeError):
                owned = False
            value["source"] = "routepilot" if owned else "external_mdm"
        value.update(
            state="available",
            time=time.time(),
            requested=preference(read_config()["main"].get("warp_protocol", "auto")),
            message="当前协议由本路由器的专用管理文件设置"
            if value["source"] == "routepilot"
            else "当前协议来自外部管理文件；本插件不会覆盖"
            if value["source"] == "external_mdm"
            else "当前协议来自组织策略；另选协议仅管理本路由器，应用后重连"
            if value["managed"]
            else "应用后才切换本路由器的协议；不会自动切换出口或调整缓冲",
        )
    except UnrecognizedProtocol:
        value = dict(state="unknown", time=time.time(), message=UNKNOWN_WARNING)
    except Exception:
        value = dict(
            state="unavailable",
            time=time.time(),
            message="尚不能读取客户端协议；请先准备并登录主线路",
        )
    atomic(STATE, json.dumps(value, ensure_ascii=False), 0o644)
    return value


def save_organization(request_id=None):
    # A server-side narrow write avoids committing unrelated form/GridSection edits.
    from runtime import command
    from worker_backend import guard
    from request_input import claim_request

    with guard(), claim_request(ORGANIZATION_INPUT, request_id, 1024) as data:
        if not isinstance(data, dict) or set(data) != {"organization"}:
            raise ControlError("组织输入无效")
        from auth_control import organization as normalize_organization

        org = normalize_organization(data["organization"])
        cfg = read_config()["main"]
        old = cfg.get("organization", "")
        if cfg.get("enabled") == "1" and org != old:
            raise ControlError("主线路正在使用，请先手动关闭再更换组织；原账户未改动")
        nonce = cfg.get("apply_nonce") or str(time.time_ns())
        if org != old or not cfg.get("apply_nonce"):
            command(["uci", "set", "routepilot.main.organization=" + org])
            command(["uci", "set", "routepilot.main.apply_nonce=" + nonce])
            command(["uci", "commit", "routepilot"])
        return dict(organization=org, nonce=nonce, saved=True, request_id=request_id.split(":")[0])


def observe():
    """Use the active data plane without switching exit, transport, or routing."""
    before = json.loads((RUN / "status.json").read_text())
    if before.get("state") != "running" or not 0 <= time.time() - before.get("time", 0) < 15:
        raise ControlError("请先确认所选线路正在运行，再检测实际出口地区")
    ip, country = probe_exit(before.get("backend", "masque"))
    after = json.loads((RUN / "status.json").read_text())
    context = (
        "runtime_id",
        "generation",
        "applied_nonce",
        "exit",
        "backend",
        "endpoint_generation",
    )
    if after.get("state") != "running" or any(before.get(k) != after.get(k) for k in context):
        raise ControlError("检测期间线路已变化；本次结果不作为当前出口证据")
    value = dict(
        state="done",
        time=time.time(),
        ip=ip,
        country=country,
        source="Cloudflare trace loc; IP geolocation may differ between websites",
        message="此连接观测到的出口地区；不代表所有网站定位，也不改变国家或出口",
        **{k: after.get(k) for k in context},
    )
    atomic(OBSERVATION, json.dumps(value, ensure_ascii=False), 0o644)
    return value


def probe_exit(backend="masque"):
    from mplane import net
    from worker_backend import curl
    from ipaddress import IPv4Address

    runner = curl if backend == "worker" else net
    args = [
        "curl",
        "-4",
        "-fsS",
        "--connect-timeout",
        "4",
        "--max-time",
        "8",
        "--max-filesize",
        "4096",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]
    if runner is net:
        args[1:1] = ["-q", "--noproxy", "*", "--interface", "if!CloudflareWARP"]
    response = runner(args)
    fields = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    ip = IPv4Address(fields.get("ip", ""))
    country = fields.get("loc", "")
    if not ip.is_global or not re.fullmatch("[A-Z]{2}", country):
        raise ControlError("检测站点未返回可识别的公网 IPv4 或地区")
    return str(ip), country
