"""Probe an existing WARP exit without installing LAN forwarding rules."""

import fcntl
import ipaddress
import json
import subprocess
import time
import uuid

from diagnostics import snapshot
from masque_config import resolve_exit
from mplane import EXIT_SELECTION_MISMATCH, cli
from public_errors import ControlError
from runtime import RUN, atomic, read_config
from warp_cli import PROTOCOLS, TunnelStatus, parse_directory, parse_selected_network, read_status
from warp_control import probe_exit

RESULT = "prepared-exit-test.json"
CONTEXT = ("apply_nonce", "exit", "organization", "warp_protocol")
PROBE_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired)


def preparing(main):
    return main.get("enabled") == "0" and main.get("preparing") == "1"


def publish(result):
    atomic(RUN / RESULT, json.dumps(result, ensure_ascii=False), 0o644)


def submit():
    main = read_config()["main"]
    status = snapshot("status.json", RUN)
    if (
        not preparing(main)
        or status.get("state") != "preparing"
        or not 0 <= time.time() - status.get("time", 0) < 15
    ):
        raise ControlError("请先准备 WARP 登录环境，等待就绪后再检测")
    try:
        exit_id = resolve_exit(main)
    except ValueError as error:
        raise ControlError(str(error)) from None
    with (RUN / "prepared-exit-test.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = snapshot(RESULT, RUN)
        if (
            current.get("state") in ("queued", "running")
            and time.time() - current.get("time", 0) < 45
        ):
            raise ControlError("路由器出口检测正在进行")
        ident = uuid.uuid4().hex
        publish(
            dict(
                state="queued", request_id=ident, time=time.time(),
                nonce=main.get("apply_nonce"), exit_id=exit_id,
            )
        )
    return dict(state="submitted", request_id=ident)


def run_pending(main, service):
    request = snapshot(RESULT, RUN)
    if request.get("state") != "queued":
        return False
    with (RUN / "prepared-exit-test.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        request = snapshot(RESULT, RUN)
        if request.get("state") != "queued":
            return False
        row = dict(
            request_id=request["request_id"], time=time.time(),
            nonce=request["nonce"], exit_id=request.get("exit_id"),
        )
        publish(dict(row, state="running"))
        message = "无法读取本次检测配置，请核对已保存出口后重试"
        try:
            vnet = resolve_exit(main)
            if (
                time.time() - request["time"] > 15
                or request["nonce"] != main.get("apply_nonce")
                or request.get("exit_id") != vnet
            ):
                raise ControlError("检测请求已过期或配置已变化，请重试")
            if service.backend != "masque" or service.compiled is not None or not preparing(main):
                raise ControlError("当前不在独立 WARP 登录环境")
            def checkpoint():
                current = read_config(timeout=2)["main"]
                if (
                    not preparing(current)
                    or any(current.get(key) != main.get(key) for key in CONTEXT)
                    or resolve_exit(current) != vnet
                ):
                    raise ControlError("检测期间配置已变化，请重试")

            checkpoint()

            def bounded_cli(*args, **kwargs):
                return cli(*args, timeout=4, **kwargs)

            before = read_status(bounded_cli)
            if before not in (TunnelStatus.CONNECTED, TunnelStatus.DISCONNECTED):
                raise ControlError("当前 WARP 状态尚未确认，请稍后重试")
            message = "无法读取 WARP 账户出口目录，请检查账户后重试"
            directory = parse_directory(bounded_cli("vnet", "--no-paginate").stdout)
            previous_exit = directory.selected
            if before is TunnelStatus.CONNECTED and previous_exit != vnet:
                raise ControlError(EXIT_SELECTION_MISMATCH)
            networks = {item["id"] for item in directory.networks}
            if vnet not in networks or (previous_exit and previous_exit not in networks):
                raise ControlError("当前账户目录无法确认已保存出口或原出口")
            try:
                message = "建立 WARP 测试隧道失败，请检查所选出口后重试"
                try:
                    service.plane.connect(
                        vnet,
                        deadline=time.monotonic() + 22,
                        checkpoint=checkpoint,
                        keep_selection=before is TunnelStatus.CONNECTED,
                    )
                except (TimeoutError, subprocess.TimeoutExpired):
                    raise ControlError("建立 WARP 测试隧道超时，请稍后重试") from None
                message = "无法通过 WARP 测试隧道读取出口检测站点，请稍后重试"
                ip, country = probe_exit()
                message = "无法复核检测后的 WARP 状态与出口，请稍后重试"
                checkpoint()
                selected = parse_selected_network(bounded_cli("vnet", "--no-paginate").stdout)
                if read_status(bounded_cli) is not TunnelStatus.CONNECTED or selected != vnet:
                    raise ControlError("检测期间 WARP 状态或出口已变化，请重试")
                expected = main.get("expected_" + main["exit"])
                message = "无法核对检测结果中的公网 IPv4 与配置的预期地址"
                if expected and ipaddress.IPv4Address(expected) != ipaddress.IPv4Address(ip):
                    raise ControlError("实际公网 IPv4 与已设置的预期地址不一致")
                protocol = service.plane.protocol
                if protocol not in PROTOCOLS:
                    raise ControlError("无法确认 WARP 的有效协议")
            finally:
                if before is TunnelStatus.DISCONNECTED:
                    try:
                        bounded_cli("disconnect")
                    except PROBE_ERRORS:
                        raise ControlError(
                            "无法确认 WARP 测试隧道已断开，请查看登录环境"
                        ) from None
                    if previous_exit and previous_exit != vnet:
                        try:
                            selected = parse_selected_network(
                                bounded_cli("vnet", "--no-paginate").stdout
                            )
                            if selected != previous_exit:
                                bounded_cli("vnet", previous_exit)
                                selected = parse_selected_network(
                                    bounded_cli("vnet", "--no-paginate").stdout
                                )
                        except PROBE_ERRORS:
                            raise ControlError(
                                "原 WARP 出口选择未确认恢复，请查看登录环境"
                            ) from None
                        if selected != previous_exit:
                            raise ControlError("原 WARP 出口选择未确认恢复，请查看登录环境")
        except ControlError as error:
            publish(dict(row, state="failed", time=time.time(), message=str(error)))
        except PROBE_ERRORS:
            publish(dict(row, state="failed", time=time.time(), message=message))
        else:
            publish(dict(row, state="done", time=time.time(), ip=ip, country=country, protocol=protocol))
        return True
