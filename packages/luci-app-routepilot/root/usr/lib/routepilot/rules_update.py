"""Rule snapshots, commits, and update lifecycle."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from contextlib import contextmanager
import time
import uuid

from runtime import atomic, read_config, RUN, RULES, validate_rule_text
from masque_config import (
    normalize_domains,
)
from mplane import net
from public_errors import ControlError


LABELS = {
    "china": "中国 IP",
    "gfw": "代理域名",
    "whitelist": "直连域名",
    "china_domains": "中国域名",
}


@contextmanager
def rule_snapshot():
    """Pin a committed generation until digest and compilation finish."""
    RULES.mkdir(parents=True, exist_ok=True)
    with open(RULES / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        from rules_catalog import resolve_current

        yield resolve_current(RULES)


def commit_rules(pending, meta, *, allow_decline=False, replace_all=False):
    """Stage a complete generation, then publish it with one rename."""
    RULES.mkdir(parents=True, exist_ok=True)
    with open(RULES / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = RULES / "current"
        from rules_catalog import resolve_current

        old = resolve_current(RULES)
        generations = RULES / ".generations"
        generations.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="rules-", dir=generations))
        link = RULES / (".current-" + uuid.uuid4().hex)
        committed = False
        try:
            if not replace_all:
                for path in old.glob("*.txt"):
                    atomic(stage / path.name, path.read_text())
            for path, text in pending:
                atomic(stage / path.name, text)
            counts = {
                path.stem: sum(
                    1
                    for line in path.read_text().splitlines()
                    if line.strip() and not line.startswith("#")
                )
                for path in stage.glob("*.txt")
            }
            from rules_catalog import enrich, declines, hold_candidate, RulesDecline

            meta = enrich(stage, old, dict(meta, counts=counts))
            atomic(stage / "updated.json", json.dumps(meta))
            drops = declines(stage, old)
            if drops and not allow_decline:
                hold_candidate(RULES, stage, meta, drops)
                raise RulesDecline("规则数量下降超过50%，整批已暂存等待确认；继续使用上一版本")
            # All files are fsynced by atomic(); persist directory entries before publish.
            fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.symlink(str(stage.relative_to(RULES)), link)
            os.replace(link, current)
            committed = True
            # Retain current and previous generations; readers hold the shared lock.
            try:
                retained = (RULES / meta["previous_path"]).resolve()
                for candidate in generations.iterdir():
                    if candidate.is_dir() and candidate.resolve() not in (
                        stage.resolve(),
                        retained,
                    ):
                        shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                pass  # A cleanup failure must not misreport an already committed update.
            return meta
        finally:
            link.unlink(missing_ok=True)
            # SIGTERM may raise immediately after rename, before committed is assigned.
            # Never delete a generation already referenced by the published pointer.
            if not committed and not (
                current.is_symlink() and current.resolve() == stage.resolve()
            ):
                shutil.rmtree(stage, ignore_errors=True)


def update_rules(*, required=None, expected_nonce=None):
    """Update the whole catalog or bootstrap missing rules for the active policies."""
    from rules_catalog import operation_lock

    with operation_lock(RULES):
        return _update_rules(required, expected_nonce)


def _update_rules(required, expected_nonce):
    from rules_catalog import RulesDecline, content, lines, read_json, sources_fingerprint
    from rules_overrides import load as load_personal, merge, validate_required

    cfg = read_config()
    bootstrapping = required is not None
    if bootstrapping and (
        cfg["main"].get("enabled") != "1"
        or cfg["main"].get("apply_nonce", "") != expected_nonce
    ):
        raise ControlError("分流设置已变化，请等待新设置生效")
    with rule_snapshot() as snapshot:
        old_meta = read_json(snapshot / "updated.json")
        if bootstrapping:
            personal = load_personal(RULES)
            # Another rule operation may have filled the missing files before we acquired the lock.
            required = {
                kind
                for kind in required
                if not merge(lines(snapshot / (kind + ".txt")), kind, personal)
            }
            if not required:
                return
    if bootstrapping:
        job = "bootstrap-" + uuid.uuid4().hex
    else:
        job = (
            os.environ.get("ROUTEPILOT_UPDATE_ID")
            or cfg["main"].get("update_nonce")
            or str(int(time.time()))
        )
    progress = dict(
        state="running",
        request_id=job,
        started_at=int(time.time()),
        finished_at=0,
        phase="准备当前分流所需的规则" if bootstrapping else "准备下载",
        percent=0,
        last_success_at=old_meta.get("downloaded_at", old_meta.get("time", 0)),
        counts=old_meta.get("counts", {}),
        ignored_regexp=old_meta.get("ignored_regexp", {}),
    )

    def publish():
        atomic(RUN / "update.json", json.dumps(progress, ensure_ascii=False), 0o644)

    publish()
    pending, ignored = [], {}
    sources = [
        (kind, cfg["main"].get(kind + "_url", ""))
        for kind in LABELS
        if (kind in required if bootstrapping else cfg["main"].get(kind + "_url"))
    ]
    label = "规则"
    try:
        for index, (kind, url) in enumerate(sources):
            label = LABELS[kind]
            progress.update(
                phase="正在下载并校验：" + label, percent=round(index / max(1, len(sources)) * 90)
            )
            publish()
            if not url:
                raise ValueError("尚未设置该规则来源地址，请先在规则与更新中配置")
            if not re.fullmatch(r"https://[^\s]{1,2000}", url):
                raise ValueError("更新地址必须使用 HTTPS")
            downloader = net
            if cfg["main"].get("backend", "masque") == "worker":
                from worker_backend import curl as downloader
            result = downloader(
                [
                    "curl",
                    "-fLsS",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "20",
                    "--max-filesize",
                    "4194304",
                    url,
                ]
            )
            if kind == "china":
                text = validate_rule_text("china", result.stdout)
            else:
                text, count = normalize_domains(result.stdout)
                ignored[kind] = count
            pending.append((RULES / (kind + ".txt"), text))
        if not pending:
            raise ValueError("尚未设置规则更新地址")
        progress.update(phase="校验完成，正在替换本插件规则", percent=95)
        publish()
        now = int(time.time())
        latest_cfg = read_config()
        if sources_fingerprint(cfg["main"]) != sources_fingerprint(latest_cfg["main"]):
            raise ValueError("规则来源设置已变化，请重新更新")
        if bootstrapping and (
            latest_cfg["main"].get("enabled") != "1"
            or latest_cfg["main"].get("apply_nonce", "") != expected_nonce
        ):
            raise ValueError("分流设置已变化，请等待新设置生效")
        with rule_snapshot() as snapshot:
            combined = content(snapshot)
            for path, text in pending:
                combined[path.stem] = text.splitlines()
            personal = load_personal(RULES)
            if bootstrapping:
                if any(not merge(combined[kind], kind, personal) for kind in required):
                    raise ValueError("个人覆盖后，当前分流所需规则为空，请核对排除项")
            else:
                validate_required(latest_cfg, combined, personal)
        last_success = progress["last_success_at"] if bootstrapping else now
        meta = commit_rules(
            pending,
            dict(
                time=now,
                downloaded_at=last_success,
                ignored_regexp={**progress["ignored_regexp"], **ignored}
                if bootstrapping
                else ignored,
                sources=dict(sources),
                sources_fingerprint=sources_fingerprint(cfg["main"]),
                request_id=job,
            ),
        )
        progress.update(
            state="partial" if bootstrapping else "success",
            phase="已保存当前分流所需规则；完整规则更新仍按计划执行"
            if bootstrapping
            else "更新成功，规则已保存",
            percent=100,
            finished_at=now,
            last_success_at=last_success,
            counts=meta["counts"],
            ignored_regexp=meta["ignored_regexp"],
        )
        publish()
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else "下载或连接失败"
        try:
            with rule_snapshot() as snapshot:
                saved = any(content(snapshot).values())
        except (OSError, ValueError):
            saved = None
        fallback = (
            "保留已保存的规则"
            if saved
            else "当前没有已保存的规则"
            if saved is False
            else "尚未确认已保存的规则"
        )
        message = label + "更新失败：" + reason + "；" + fallback + "，可重新更新"

        progress.update(
            state="blocked" if isinstance(error, RulesDecline) else "failed",
            phase=message,
            finished_at=int(time.time()),
        )
        publish()
        if bootstrapping:
            raise ControlError("当前分流所需规则未就绪。" + message) from None
        raise


def finish_interrupted_update():
    """Do not leave a dead worker's progress permanently running."""
    try:
        path = RUN / "update.json"
        state = json.loads(path.read_text())
        if state.get("state") == "running":
            state.update(
                state="unknown",
                phase="更新任务已中断，发布结果未知；请核对已保存的规则后重试",
                finished_at=int(time.time()),
            )
            atomic(path, json.dumps(state, ensure_ascii=False), 0o644)
    except (OSError, ValueError):
        pass
