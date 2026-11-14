"""Coordinate the main routing service and report its state."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from runtime import atomic, read_config, RUN, RULES
import speed_compare
import prepared_exit
from masque_config import (
    EmptyChinaRules,
    compile_config,
    live_lan_networks,
    selected_lans,
    resolve_exit,
)
from mplane import Plane
from connection_events import record
from diagnostics import AutomaticCheck
from egress_guard import boot_config
from worker_backend import WorkerLineFailure, WorkerPlane, WorkerStartupFailure
from warp_cli import UNKNOWN_WARNING, UnknownTunnelStatus
from warp_bootstrap_dns import FIELDS as WARP_DNS_FIELDS

from lan_discovery import lan_interfaces
from rules_update import rule_snapshot, update_rules, finish_interrupted_update
from service import (
    CaptureUnavailable,
    FALLBACK_MESSAGE,
    UNKNOWN_STATUS_SECONDS,
    Service,
    TunnelPending,
    guard_fallback,
    preflight,
    routing_digest,
    waiting_device_message,
)

STOP = False
ENTRYPOINT = Path(__file__).with_name("masque.py")
_last_event = None


def output(state, message, devices=(), **extra):
    atomic(
        RUN / "status.json",
        json.dumps(
            dict(
                state=state, message=message, devices=list(devices), time=int(time.time()), **extra
            ),
            ensure_ascii=False,
        ),
        0o644,
    )
    global _last_event
    event = (state, extra.get("recovering", False), extra.get("fallback"), extra.get("runtime_id"))
    if event != _last_event:
        # Only fixed public state and context; never raw errors or identity.
        record(
            RUN,
            "events",
            dict(
                state=state,
                recovering=bool(extra.get("recovering")),
                fallback=extra.get("fallback"),
                backend=extra.get("backend"),
                runtime_id=extra.get("runtime_id"),
            ),
        )
        _last_event = event


class ConfigCheckError(Exception):
    """A failure during configuration checks, retaining the original error."""

    def __init__(self, original):
        super().__init__(str(original))
        self.original = original


def validate_configuration(cfg, service, schedule, endpoint_manager):
    if not service.update_job:
        try:
            schedule.reconcile(cfg["main"])
        except OSError:
            pass  # A rule schedule write failure must not tear down a healthy tunnel.
    try:
        from request_input import reap_requests

        reap_requests(RUN)
        endpoint_manager.poll()
    except Exception:
        pass  # Candidate refresh must never interrupt the data plane.
    enabled = cfg["main"].get("enabled") == "1"
    if enabled:
        service.egress_guard.configure(cfg)
        service.egress_guard.require_supported()
        cfg["main"]["lan_networks"] = live_lan_networks(selected_lans(cfg["main"]), lan_interfaces())
    preparing = cfg["main"].get("preparing") == "1"
    return enabled, preparing


def handle_disabled(cfg, service):
    stopped = service.stop()
    if not stopped["direct"]:
        raise RuntimeError("关闭清理未能确认；" + stopped["legacy"])
    service.egress_guard.configure(cfg)
    output(
        "disabled",
        "分流已关闭；" + stopped["legacy"],
        egress_guard=service.egress_guard.state,
        applied_nonce=cfg["main"].get("apply_nonce", ""),
    )


def handle_preparing(cfg, service, schedule, last_nonce):
    output(
        "preparing",
        "登录环境已就绪；请完成邮箱登录，再自动读取出口",
        warp_dns=getattr(service.plane, "warp_dns", None),
        egress_guard=service.egress_guard.state,
        applied_nonce=cfg["main"].get("apply_nonce", ""),
    )
    if prepared_exit.run_pending(cfg["main"], service):
        return last_nonce
    nonce = cfg["main"].get("update_nonce", "")
    if nonce and nonce != last_nonce:
        try:
            schedule.start(cfg["main"], nonce)
            vnet = resolve_exit(cfg["main"])
            if service.backend != "worker":
                service.plane.protocol_preference = cfg["main"].get("warp_protocol", "auto")
            service.plane.connect(vnet)
            update_rules()
            schedule.finish(cfg["main"], True)
            output("preparing", "规则更新完成；设备分流仍关闭")
        except Exception:
            progress = __import__("rules_schedule").read(RUN / "update.json")
            if progress.get("request_id") != nonce or progress.get("state") == "running":
                atomic(
                    RUN / "update.json",
                    json.dumps(
                        dict(
                            state="failed",
                            request_id=nonce,
                            phase="更新未开始：请确认登录环境和出口已就绪；保留原规则",
                            started_at=int(schedule.data.get("last_attempt_at", time.time())),
                            finished_at=int(time.time()),
                            last_success_at=schedule.plan(cfg["main"])["last_success_at"],
                            counts=progress.get("counts", {}),
                        )
                    ),
                    0o644,
                )
                progress = {}
            schedule.finish(
                cfg["main"],
                False,
                blocked=progress.get("state") == "blocked",
                request_id=nonce,
            )
            output(
                "preparing",
                "更新未完成，请先完成组织登录并填写出口 ID；设备分流仍关闭",
            )
        last_nonce = nonce
    return last_nonce


def apply_running_configuration(cfg, service, previous):
    from rules_catalog import directory_revision
    from rules_overrides import effective_revision, load as load_personal

    waiting = preflight(cfg)
    waiting_message = waiting_device_message(waiting)
    needed = {
        "bypass_cn": "china",
        "gfw": "gfw",
        "whitelist": "whitelist",
    }
    policies = (
        [{"enabled": "1", "mode": cfg["main"].get("lan_mode", "bypass_cn")}]
        if cfg["main"].get("scope") == "lan"
        else [policy for policy in cfg["policies"] if policy["id"] not in waiting]
    )
    required = {
        needed[mode]
        for p in policies
        if p.get("enabled") == "1" and (mode := p.get("mode", "bypass_cn")) in needed
    }
    for allow_bootstrap in (True, False):
        with rule_snapshot() as snapshot:
            cfg["rule_overrides"] = load_personal(RULES)
            missing_rules = set()
            if allow_bootstrap:
                missing_rules = {
                    kind
                    for kind in required
                    if not (snapshot / (kind + ".txt")).exists()
                    and not cfg["rule_overrides"][kind]["added"]
                }
            if not missing_rules:
                config_digest = routing_digest(cfg, snapshot)
                digest = config_digest
                if waiting:
                    digest = hashlib.sha256(
                        (config_digest + ":" + ",".join(sorted(waiting))).encode()
                    ).hexdigest()
                try:
                    compiled = compile_config(cfg, snapshot, waiting) if digest != previous else None
                except EmptyChinaRules:
                    if not allow_bootstrap:
                        raise
                    missing_rules = {"china"}
                else:
                    if compiled is not None:
                        compiled["rules_revision"] = effective_revision(
                            directory_revision(snapshot), cfg["rule_overrides"]
                        )
                    break
        # Download outside the shared snapshot lock. Other sources stay on their schedule.
        vnet = "" if service.backend == "worker" else resolve_exit(cfg["main"])
        service.plane.connect(vnet)
        update_rules(required=missing_rules, expected_nonce=cfg["main"].get("apply_nonce", ""))
    if compiled is not None:
        compiled["config_digest"] = config_digest
        compiled["dns"]["generation"] = digest
        if waiting and not compiled["dns"]["policies"]:
            if not service.withdraw():
                raise RuntimeError("等待设备上线时，未能确认旧接管规则已撤回")
        else:
            service.apply(cfg, compiled)
        previous = digest
    return previous, waiting, waiting_message, digest


def schedule_update(cfg, service, schedule, last_nonce, update_message, update_job_id):
    nonce = cfg["main"].get("update_nonce", "")
    try:
        if service.update_job and service.update_job.poll() is not None:
            succeeded = service.update_job.returncode == 0
            finish_interrupted_update()
            progress = __import__("rules_schedule").read(RUN / "update.json")
            blocked = progress.get("state") == "blocked"
            completed = schedule.finish(
                cfg["main"],
                succeeded,
                blocked=blocked,
                request_id=update_job_id or schedule.data.get("request_id"),
            )
            update_message = (
                "规则已保存，等待运行版本确认"
                if succeeded
                else "规则异常，等待确认"
                if blocked
                else "更新失败，保留现有规则并按计划重试"
            )
            if completed is False:
                update_message = "规则状态已由后续确认或回退操作更新"
            service.update_job = None
            update_job_id = None
        if not service.update_job and schedule.due(cfg["main"]):
            job = (
                nonce
                if nonce and nonce != schedule.data.get("last_nonce")
                else "auto-" + uuid.uuid4().hex
            )
            schedule.start(cfg["main"], job)
            update_job_id = job
            try:
                service.update_job = subprocess.Popen(
                    [sys.executable, str(ENTRYPOINT), "update-worker"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "ROUTEPILOT_UPDATE_ID": job},
                )
            except Exception:
                schedule.finish(cfg["main"], False)
                update_message = "更新任务未启动，保留现有规则并按计划重试"
            else:
                update_message = "规则正在后台更新"
                last_nonce = nonce
    except OSError:
        update_message = "更新计划无法持久保存，保留当前线路；请检查存储空间"
    return last_nonce, update_message, update_job_id


def publish_running(
    cfg, service, schedule, waiting, waiting_message, digest, guard_wait, update_message
):
    scope_message = (
        (
            "所选局域网 "
            + "、".join(v["interface"] + " " + v["network"] for v in cfg["main"]["lan_networks"])
        )
        if service.compiled.get("scope") == "lan"
        else "仅接管所选设备"
    )
    unknown_since = getattr(service, "unknown_since", None)
    tunnel = {}
    if service.backend == "worker":
        message = "备用线路运行中（TCP，UDP不代理）；"
    elif unknown_since is not None:
        elapsed = time.monotonic() - unknown_since
        message = (
            UNKNOWN_WARNING + "；"
            if elapsed >= UNKNOWN_STATUS_SECONDS
            else "主线路出口已通过严格探测；客户端状态待识别；"
        )
        tunnel = {"tunnel_status": "unknown", "tunnel_warning": UNKNOWN_WARNING}
    else:
        message = (service.plane.protocol or "WARP") + " 已连接；"
    output(
        "running",
        message + scope_message + "。" + guard_wait + update_message,
        service.compiled["devices"],
        pending_devices=waiting,
        device_wait_message=waiting_message,
        egress_guard=service.egress_guard.state,
        exit=service.compiled["exit"],
        exit_ip=service.exit_ip,
        runtime_id=service.runtime_id,
        generation=digest,
        protocol="Workers" if service.backend == "worker" else (service.plane.protocol or "WARP"),
        endpoint_generation=__import__("endpoint_control").generation(),
        protocol_managed=getattr(service.plane, "protocol_managed", None),
        protocol_warning=getattr(service.plane, "protocol_warning", ""),
        protocol_observed_at=getattr(service.plane, "protocol_observed_at", 0),
        warp_dns=getattr(service.plane, "warp_dns", None),
        backend=service.backend,
        proxy_interface="rp-backup" if service.backend == "worker" else "rp-host",
        backup_name=service.plane.node["name"]
        if service.backend == "worker" and service.plane.node
        else None,
        scope=service.compiled.get("scope", "devices"),
        rules_updated=int(schedule.data.get("last_success_at", 0)),
        rules_revision=service.compiled.get("rules_revision", ""),
        applied_nonce=cfg["main"].get("apply_nonce", ""),
        **tunnel,
    )


def run():
    lock = open(RUN / "masque.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    def halt(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    from endpoint_control import Manager, stage_restored_preferences

    stage_restored_preferences()
    endpoint_manager = Manager()
    service = Service()
    connectivity_check = AutomaticCheck(RUN)
    boot = boot_config()
    if boot["main"].get("enabled") == "1":
        service.egress_guard.configure(boot)
    service.stop(restore=False)  # Recover owned resources without undoing fault fallback.
    speed_compare.discard_interrupted()
    comparison = None
    previous = None
    endpoint_paused = False
    pause_cleanup = {"direct": False}
    from rules_schedule import Schedule

    schedule = Schedule(rules=RULES, run=RUN)
    last_nonce, update_message = schedule.data.get("last_nonce"), ""
    update_job_id = None
    retried_worker_nonce = None
    try:
        while not STOP:
            cfg = None
            try:
                speed_compare.expire_queued()
                try:
                    cfg = read_config()
                    enabled, preparing = validate_configuration(
                        cfg, service, schedule, endpoint_manager
                    )
                except TunnelPending:
                    raise
                except Exception as error:
                    raise ConfigCheckError(error) from error
                if endpoint_manager.recover_requested(service, cfg["main"]):
                    previous = None
                if not enabled and not preparing:
                    handle_disabled(cfg, service)
                    if comparison:
                        speed_compare.finish(comparison, restored=True)
                        comparison = None
                    previous = None
                else:
                    if not enabled:
                        service.egress_guard.configure(cfg)
                    desired = cfg["main"].get("backend", "masque") if enabled else "masque"
                    if desired not in ("masque", "worker"):
                        raise ValueError("线路类型无效")
                    if desired == "masque" and enabled:
                        reason = endpoint_manager.pause_reason(cfg["main"])
                        if reason:
                            if not endpoint_paused:
                                pause_cleanup = service.stop(restore=False)
                                endpoint_paused = True
                            previous = None
                            if comparison:
                                speed_compare.finish(comparison, restored=False)
                                comparison = None
                                reason += "；发生在线路对比测试恢复期间"
                            output(
                                "error",
                                reason,
                                paused=True,
                                recovering=False,
                                fallback=guard_fallback(
                                    service.egress_guard, pause_cleanup["direct"]
                                ),
                                egress_guard=service.egress_guard.state,
                                scope=cfg["main"].get("scope", "devices"),
                                attempted_nonce=cfg["main"].get("apply_nonce", ""),
                            )
                            time.sleep(3)
                            continue
                    endpoint_paused = False
                    if desired == "worker":
                        from worker_backend import check_retry

                        if service.worker_failed_nonce == cfg["main"].get("apply_nonce", ""):
                            raise ValueError("备用线路已失败；请重新检测后手动启用，或切回主线路")
                        check_retry(cfg["main"].get("apply_nonce", ""))
                    if desired != service.backend:
                        if not service.stop(restore=False)["direct"]:
                            raise RuntimeError("旧线路清理未能确认；未切换备用")
                        service.plane = WorkerPlane() if desired == "worker" else Plane()
                        service.backend = desired
                        previous = None
                    if desired != "worker":
                        from warp_control import preference

                        requested = preference(cfg["main"].get("warp_protocol", "auto"))
                        if service.plane.owned and service.plane.protocol_preference != requested:
                            if not service.stop(restore=False)["direct"]:
                                raise RuntimeError("协议重连前清理未能确认")
                            service.plane = Plane()
                            previous = None
                        service.plane.protocol_preference = requested
                        reselect = RUN / "warp-dns-reselect"
                        if reselect.exists():
                            reselect.unlink(missing_ok=True)
                            if service.plane.owned:
                                if not service.stop(restore=False)["direct"]:
                                    raise RuntimeError("WARP 重建前清理未能确认")
                                service.plane = Plane()
                                service.plane.protocol_preference = requested
                                previous = None
                        service.plane.organization = cfg["main"].get("organization", "")
                        service.plane.dns_preferences = {
                            key: cfg["main"][key] for key in WARP_DNS_FIELDS if key in cfg["main"]
                        }
                    if desired != "worker" or service.compiled is None:
                        service.plane.start()
                    elif not service.plane.running:
                        # Restarting before health() would hide the exit and lose the TUN route.
                        raise WorkerLineFailure("备用核心已退出")
                    try:
                        if enabled and endpoint_manager.tick(service, cfg["main"]):
                            previous = None
                    except BlockingIOError:
                        pass
                    if (
                        enabled
                        and service.backend != "worker"
                        and service.recovery_started is not None
                    ):
                        service.tunnel_ready()
                    if not enabled:
                        last_nonce = handle_preparing(cfg, service, schedule, last_nonce)
                    else:
                        previous, waiting, waiting_message, digest = apply_running_configuration(
                            cfg, service, previous
                        )
                        if waiting and service.compiled is None:
                            if comparison:
                                speed_compare.finish(comparison, restored=False)
                                comparison = None
                                waiting_message += "；发生在线路对比测试恢复期间"
                            output(
                                "waiting_devices",
                                "暂无已核验的代理设备。" + waiting_message,
                                pending_devices=waiting,
                                egress_guard=service.egress_guard.state,
                                scope="devices",
                                applied_nonce=cfg["main"].get("apply_nonce", ""),
                            )
                            time.sleep(3)
                            continue
                        service.health()
                        guard_wait = (
                            "受保护设备正在等待 DNS 就绪。"
                            if service.confirm_egress_ready() is False
                            else ""
                        )
                        if comparison and not guard_wait:
                            speed_compare.finish(comparison, restored=True)
                            comparison = None
                        elif comparison:
                            if time.monotonic() >= comparison["restore_deadline"]:
                                raise RuntimeError("DNS 就绪超时")
                            output(
                                "testing",
                                "正在恢复保存的线路配置；" + guard_wait,
                                test_id=comparison["id"],
                                egress_guard=service.egress_guard.state,
                            )
                            time.sleep(1)
                            continue
                        if (
                            not comparison
                            and desired == "masque"
                            and not guard_wait
                            and speed_compare.REQUEST.exists()
                        ):

                            def show_target(job, target, index, total):
                                output(
                                    "testing",
                                    "正在对比线路；设备接管暂时撤回",
                                    test_id=job,
                                    target=target,
                                    index=index,
                                    total=total,
                                    egress_guard=service.egress_guard.state,
                                )

                            comparison = speed_compare.execute(
                                service, cfg, lambda: STOP, show_target
                            )
                            if comparison:
                                comparison["restore_deadline"] = time.monotonic() + 30
                                previous = None
                                continue
                        last_nonce, update_message, update_job_id = schedule_update(
                            cfg, service, schedule, last_nonce, update_message, update_job_id
                        )
                        publish_running(
                            cfg,
                            service,
                            schedule,
                            waiting,
                            waiting_message,
                            digest,
                            guard_wait,
                            update_message,
                        )
                        if not guard_wait:
                            try:
                                connectivity_check.tick()
                            except OSError:
                                pass  # Diagnostic startup must never withdraw a working route.
            except (TunnelPending, UnknownTunnelStatus) as error:
                try:
                    direct = service.suspend_capture()
                except Exception:
                    direct = service.stop(restore=False)["direct"]
                previous = None
                fallback = guard_fallback(service.egress_guard, direct)
                unknown = isinstance(error, UnknownTunnelStatus)
                tunnel = (
                    {"tunnel_status": "unknown", "tunnel_warning": UNKNOWN_WARNING}
                    if unknown
                    else {}
                )
                message = (
                    UNKNOWN_WARNING + "；等待确认出口；"
                    if unknown
                    else "WARP 暂时中断，等待原客户端恢复；"
                )
                if comparison:
                    speed_compare.finish(comparison, restored=False)
                    comparison = None
                    message += "发生在线路对比测试恢复期间；"
                output(
                    "error",
                    message + FALLBACK_MESSAGE[fallback],
                    fallback=fallback,
                    egress_guard=service.egress_guard.state,
                    recovering=True,
                    scope=cfg["main"].get("scope", "devices"),
                    attempted_nonce=cfg["main"].get("apply_nonce", ""),
                    **tunnel,
                )
            except Exception as error:
                failure = error.original if isinstance(error, ConfigCheckError) else error
                main_config = cfg["main"] if cfg is not None else {}
                worker_selected = (
                    main_config.get("enabled") == "1" and main_config.get("backend") == "worker"
                )
                worker_active = worker_selected and service.backend == "worker"
                nonce = main_config.get("apply_nonce", "")
                retry_startup = (
                    worker_active
                    and isinstance(failure, WorkerStartupFailure)
                    and retried_worker_nonce != nonce
                )
                if retry_startup:
                    retried_worker_nonce = nonce
                elif worker_active and isinstance(failure, WorkerLineFailure):
                    from worker_backend import mark_failed

                    service.worker_failed_nonce = nonce
                    try:
                        mark_failed(nonce)
                    except OSError:
                        pass
                local_capture_failure = isinstance(failure, CaptureUnavailable)
                keep_worker = (
                    service.backend == "worker"
                    and service.plane.running
                    and (cfg is None or worker_selected)
                    and not isinstance(failure, WorkerLineFailure)
                    and (isinstance(error, ConfigCheckError) or local_capture_failure)
                )
                direct = False
                if keep_worker:
                    try:
                        if local_capture_failure:
                            direct = failure.withdrawn or service.suspend_capture(
                                protection_reason="capture_unavailable"
                            )
                        else:
                            direct = service.suspend_capture()
                    except Exception:
                        pass
                if not direct:
                    direct = service.stop(restore=False)["direct"]
                previous = None
                # Known validation messages contain no credential values.
                message = (
                    str(failure)
                    if isinstance(failure, (ValueError, RuntimeError))
                    else "服务尚未就绪，请检查依赖"
                )
                if cfg is None:
                    message = "暂时无法读取 imOK 配置，稍后自动重试"
                if comparison:
                    speed_compare.finish(comparison, restored=False)
                    comparison = None
                    message += "；发生在线路对比测试恢复期间"
                scope = main_config.get("scope", "devices")
                target = "所选局域网" if scope == "lan" else "所选设备"
                fallback = guard_fallback(service.egress_guard, direct)
                output(
                    "error",
                    message + "；" + target + "：" + FALLBACK_MESSAGE[fallback],
                    fallback=fallback,
                    egress_guard=service.egress_guard.state,
                    scope=scope,
                    attempted_nonce=main_config.get("apply_nonce", ""),
                )
            # A short wait leaves time for status, strict probe, and the next lease renewal.
            time.sleep(1 if getattr(service, "unknown_since", None) is not None else 3)
    finally:
        if comparison:
            speed_compare.finish(comparison, restored=False)
        stopped = service.stop(restore=STOP)
        output(
            "stopped",
            "插件已停止；"
            + stopped["legacy"]
            + ("；接管已撤回" if stopped["direct"] else "；清理未能确认，网络路径未知"),
            fallback=guard_fallback(service.egress_guard, False),
            egress_guard=service.egress_guard.state,
        )
        lock.close()
