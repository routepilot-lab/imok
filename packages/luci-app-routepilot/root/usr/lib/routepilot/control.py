"""Local CLI validation and command dispatch."""

import fcntl
from ipaddress import IPv4Network
import json
import os
import signal
import sys
import time

from runtime import command, read_config, RUN
from egress_guard import boot_config, scope as guard_scope, validate_activation

from functools import partial
from importlib import import_module
from lan_discovery import lan_interfaces
from masque_config import device_lan, live_lan_networks, selected_lans
from public_errors import ControlError
from service import Service
from supervisor import run
from rules_update import update_rules


def interfaces():
    print(json.dumps(lan_interfaces(include_inactive=True)))


def guard_preflight():
    validate_activation()
    print("断网保护的流量卸载检查已通过")


def set_enabled(action):
    if action == "enable":
        if guard_scope(boot_config()):
            validate_activation()
        cfg = read_config()
        main = cfg["main"]
        try:
            rows = live_lan_networks(selected_lans(main), lan_interfaces())
            if main.get("scope", "devices") == "devices":
                policies = [row for row in cfg["policies"] if row.get("enabled") == "1"]
                if not any(row.get("mode") != "direct" for row in policies):
                    raise ValueError("请在「设备」启用至少一条代理策略")
                networks = [(row["interface"], IPv4Network(row["network"])) for row in rows]
                for row in policies:
                    device_lan(row.get("ip", ""), networks)
        except ValueError as error:
            raise ControlError("未开启 imOK。" + str(error)) from None
    nonce = str(time.time_ns())
    command(["uci", "set", "routepilot.main.enabled=" + ("1" if action == "enable" else "0")])
    command(["uci", "set", "routepilot.main.preparing=0"])
    command(["uci", "set", "routepilot.main.apply_nonce=" + nonce])
    command(["uci", "commit", "routepilot"])
    print(json.dumps({"enabled": action == "enable", "nonce": nonce}))


def apply_settings():
    command(["uci", "commit", "routepilot"])
    print("本插件配置已提交")


def prepare():
    command(["uci", "set", "routepilot.main.preparing=1"])
    command(["uci", "commit", "routepilot"])
    print("登录环境准备中，请稍候查看状态")


def run_test(action):
    from diagnostics import start

    start(action.removeprefix("test-") if action != "test" else "all")
    print("HTTPS 连通检测已开始")


def set_backup(action):
    from worker_backend import require_test
    from worker_backend import guard

    with guard():
        if action == "backup-enable":
            require_test()
            if read_config()["main"].get("enabled") != "1":
                raise ValueError("请先核对并开启 imok 总开关")
        nonce = str(time.time_ns())
        command(
            [
                "uci",
                "set",
                "routepilot.main.backend=" + ("worker" if action == "backup-enable" else "masque"),
            ]
        )
        command(["uci", "set", "routepilot.main.apply_nonce=" + nonce])
        command(["uci", "commit", "routepilot"])
        if action == "backup-enable":
            (RUN / "backup-failure.json").unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "nonce": nonce,
                    "backend": "worker" if action == "backup-enable" else "masque",
                }
            )
        )


def cleanup():
    with open(RUN / "masque.lock", "a") as lock:
        # procd requests a stop before the supervisor completes its final cleanup.
        deadline = time.monotonic() + 8
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SystemExit("主进程仍在运行；未并发清理网络") from None
                time.sleep(min(0.2, remaining))
        if not Service().stop()["direct"]:
            raise SystemExit("无法确认已撤回 imOK 网络接管；请检查路由器状态")


def update_worker():
    def interrupted(*_):
        raise RuntimeError("update interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        update_rules()
    except Exception:
        raise SystemExit(1)


def json_handler(module, name, *, arguments=(), request_id=False, ensure_ascii=False):
    def handle(request=None):
        target = getattr(import_module(module), name)
        values = (*arguments, request) if request_id else arguments
        print(json.dumps(target(*values), ensure_ascii=ensure_ascii))

    return handle, request_id


def login():
    from auth_control import login as authenticate

    print(authenticate())


COMMANDS = {
    "run": (run, False),
    "cleanup": (cleanup, False),
    "update-worker": (update_worker, False),
    "rules-info": json_handler("rules_catalog", "info"),
    "rules-query": json_handler("rules_catalog", "query", request_id=True),
    "rules-accept": json_handler("rules_catalog", "accept", request_id=True),
    "rules-rollback": json_handler("rules_catalog", "rollback", request_id=True),
    "rules-edit": json_handler("rules_catalog", "edit", request_id=True),
    "settings-export": json_handler("config_backup", "export"),
    "settings-import": json_handler("config_backup", "import_settings", request_id=True),
    "endpoint-info": json_handler("endpoint_control", "info"),
    "endpoint-refresh": json_handler("endpoint_control", "refresh"),
    "endpoint-save": json_handler("endpoint_control", "save", request_id=True),
    "endpoint-schedule": json_handler("endpoint_control", "save_schedule", request_id=True),
    "endpoint-recover": json_handler("endpoint_control", "save_recovery", request_id=True),
    "warp-info": json_handler("warp_control", "refresh"),
    "exit-observe": json_handler("warp_control", "observe"),
    "prepared-exit-test": json_handler("prepared_exit", "submit"),
    "warp-org-save": json_handler("warp_control", "save_organization", request_id=True),
    "backup-import": json_handler("worker_backend", "import_node", request_id=True),
    "backup-test": json_handler("worker_backend", "start_test", ensure_ascii=True),
    "backup-enable": (partial(set_backup, "backup-enable"), False),
    "backup-disable": (partial(set_backup, "backup-disable"), False),
    "backup-scan": json_handler("worker_scan", "start"),
    "backup-scan-apply": json_handler("worker_scan", "apply_best"),
    "cloud-save": json_handler("cloud_worker", "save", request_id=True),
    "cloud-target": json_handler("cloud_worker", "start", arguments=("target",)),
    "cloud-verify": json_handler("cloud_worker", "start", arguments=("verify",)),
    "cloud-deploy": json_handler("cloud_worker", "start", arguments=("deploy",)),
    "cloud-check": json_handler("cloud_worker", "start", arguments=("check",)),
    "cloud-promote": json_handler("cloud_worker", "promote"),
    "cloud-forget": json_handler("cloud_worker", "forget"),
    "relay-save": json_handler("relay_pool", "save", request_id=True),
    "relay-test": json_handler("relay_pool", "start"),
    "relay-apply": json_handler("relay_pool", "apply"),
    "relay-status": json_handler("relay_pool", "runtime_status"),
    "interfaces": (interfaces, False),
    "guard-preflight": (guard_preflight, False),
    "enable": (partial(set_enabled, "enable"), False),
    "disable": (partial(set_enabled, "disable"), False),
    "apply": (apply_settings, False),
    "prepare": (prepare, False),
    "login": (login, False),
    "register": json_handler("auth_control", "register", request_id=True),
    "discover": json_handler("exits", "read_networks"),
    "lan-dns-domestic": json_handler(
        "warp_bootstrap_dns", "start_lan_test", arguments=("domestic",)
    ),
    "lan-dns-foreign": json_handler("warp_bootstrap_dns", "start_lan_test", arguments=("foreign",)),
    "speed-compare": json_handler("speed_compare", "start", request_id=True),
    "speed-compare-cancel": json_handler("speed_compare", "cancel"),
    "endpoint-quality": json_handler("endpoint_quality", "start"),
    "speed-test": json_handler("speed_test", "start"),
    "dns-test": json_handler("dns_diagnostics", "start"),
    "dns-clear": json_handler("dns_runtime", "request_clear"),
    "warp-dns-validate": json_handler(
        "warp_bootstrap_dns", "validate_preferences", request_id=True
    ),
    "warp-dns-test": json_handler("warp_bootstrap_dns", "start_manual_test", request_id=True),
    "warp-dns-reselect": json_handler("warp_bootstrap_dns", "request_reselect"),
    **{
        name: (partial(run_test, name), False)
        for name in (
            "test",
            "test-google",
            "test-youtube",
            "test-github",
            "test-taobao",
            "test-baidu",
        )
    },
}


def control(action, request_id=None):
    try:
        handler, accepts_request_id = COMMANDS[action]
    except KeyError:
        raise ValueError("无效操作") from None
    if accepts_request_id:
        return handler(request_id)
    return handler()


def main():
    args = sys.argv[1:]
    if (
        len(args) not in (1, 2)
        or args[0] not in COMMANDS
        or len(args) == 2
        and not COMMANDS[args[0]][1]
    ):
        raise SystemExit("Usage: routepilot run|prepare|login|register")
    os.umask(0o077)
    RUN.mkdir(parents=True, exist_ok=True)
    RUN.chmod(0o755)
    from request_input import prepare_requests

    prepare_requests(RUN)
    if args[0] in ("run", "cleanup", "update-worker"):
        control(args[0])
        return
    try:
        control(*args)
    except Exception as error:
        from cloud_worker import CloudError
        from relay_pool import RelayError
        from public_errors import ControlError

        if isinstance(error, (CloudError, RelayError, ControlError)):
            raise SystemExit(str(error))
        if isinstance(error, BlockingIOError):
            raise SystemExit("另一个操作正在进行，请稍后再试")
        raise SystemExit("操作未完成，请核对服务状态和登录有效期；未显示凭据")
