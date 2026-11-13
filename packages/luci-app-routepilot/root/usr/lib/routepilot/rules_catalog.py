"""Bounded rule inspection and revision-bound local publication controls."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

from runtime import atomic
from public_errors import ControlError
from request_input import claim_request

KINDS = {
    "china": "中国 IP",
    "china_domains": "中国域名",
    "gfw": "代理域名",
    "whitelist": "直连域名",
}
CANDIDATE_TTL = 86400


class RulesDecline(ValueError):
    pass


def read_json(path, default=None):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def safe_source(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme != "https" or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        # Query/userinfo/fragment may contain credentials. Paths remain useful
        # to identify the published list, but obvious embedded secrets do not.
        if re.search(r"(token|secret|password|api[_-]?key)", parsed.path, re.I):
            return urlunsplit(("https", host, "/", "", ""))
        return urlunsplit(("https", host, parsed.path, "", ""))
    except (ValueError, TypeError):
        return ""


def sources_fingerprint(main):
    return hashlib.sha256(
        json.dumps({kind: main.get(kind + "_url", "") for kind in KINDS}, sort_keys=True).encode()
    ).hexdigest()


def resolve_current(rules):
    current = rules / "current"
    if current.is_symlink():
        try:
            path = current.resolve(strict=True)
            relative = str(path.relative_to(rules.resolve()))
        except (OSError, ValueError):
            raise ControlError("规则版本指针无效，请检查本机规则目录") from None
        if not re.fullmatch(r"\.generations/rules-[A-Za-z0-9_-]+", relative) or not path.is_dir():
            raise ControlError("规则版本指针无效，请检查本机规则目录")
        return path
    if current.exists():
        raise ControlError("规则版本指针无效，请检查本机规则目录")
    return rules


def lines(path):
    try:
        return sorted(
            set(
                v.strip()
                for v in path.read_text().splitlines()
                if v.strip() and not v.lstrip().startswith("#")
            )
        )
    except FileNotFoundError:
        return []


def content(directory):
    return {kind: lines(directory / (kind + ".txt")) for kind in KINDS}


def revision(rows):
    digest = hashlib.sha256()
    for kind in KINDS:
        digest.update((kind + "\0" + "\n".join(rows[kind]) + "\0").encode())
    return digest.hexdigest()


def directory_revision(directory, verify=False):
    stored = read_json(directory / "updated.json")
    value = stored.get("revision", "")
    if not verify and isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value):
        return value
    return revision(content(directory))


def describe(directory, verify=False):
    stored = read_json(directory / "updated.json")
    value = stored.get("revision", "")
    complete = (
        isinstance(value, str)
        and re.fullmatch("[a-f0-9]{64}", value)
        and isinstance(stored.get("counts"), dict)
        and isinstance(stored.get("hashes"), dict)
    )
    rows = None if complete and not verify else content(directory)
    counts = (
        stored["counts"] if rows is None else {kind: len(values) for kind, values in rows.items()}
    )
    hashes = (
        stored["hashes"]
        if rows is None
        else {
            kind: hashlib.sha256(
                ("\n".join(values) + ("\n" if values else "")).encode()
            ).hexdigest()
            for kind, values in rows.items()
        }
    )
    return dict(
        revision=value if rows is None else revision(rows),
        time=stored.get("time", 0),
        counts={kind: counts.get(kind, 0) for kind in KINDS},
        sources={
            kind: safe_source(value)
            for kind, value in stored.get("sources", {}).items()
            if kind in KINDS
        },
        hashes=hashes,
        changes={
            kind: stored.get("changes", {}).get(kind, {"added": 0, "removed": 0}) for kind in KINDS
        },
        ignored_regexp=stored.get("ignored_regexp", {}),
    )


@contextmanager
def operation_lock(rules):
    rules.mkdir(parents=True, exist_ok=True)
    with (rules / ".update-lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def enrich(stage, old, meta):
    """Called under the publication lock before swapping a generation."""
    before, after = content(old), content(stage)
    diff = stage / ".diff"
    diff.mkdir(exist_ok=True)
    changes = {}
    for kind in KINDS:
        added = sorted(set(after[kind]) - set(before[kind]))
        removed = sorted(set(before[kind]) - set(after[kind]))
        changes[kind] = dict(added=len(added), removed=len(removed))
        atomic(diff / (kind + "-added"), "\n".join(added) + ("\n" if added else ""))
        atomic(diff / (kind + "-removed"), "\n".join(removed) + ("\n" if removed else ""))
    stored = read_json(old / "updated.json")
    parent = stage.parent.parent.resolve()
    previous = str(old.resolve().relative_to(parent))
    if revision(after) == revision(before):
        retained = previous_path(parent, revision(before))
        if retained is not None:
            previous = str(retained.resolve().relative_to(parent))
    sources = dict(stored.get("sources", {}))
    sources.update(meta.get("sources", {}))
    sources = {kind: safe_source(value) for kind, value in sources.items() if kind in KINDS}
    return dict(
        meta,
        revision=revision(after),
        base_revision=revision(before),
        previous_path=previous,
        sources=sources,
        hashes={
            kind: hashlib.sha256(
                ("\n".join(values) + ("\n" if values else "")).encode()
            ).hexdigest()
            for kind, values in after.items()
        },
        changes=changes,
    )


def declines(stage, old):
    before, after = content(old), content(stage)
    return {
        kind: dict(before=len(before[kind]), after=len(after[kind]))
        for kind in KINDS
        if len(before[kind]) >= 100 and len(after[kind]) < len(before[kind]) * 0.5
    }


def hold_candidate(rules, stage, meta, drops):
    """Persist the entire validated batch without changing the active pointer."""
    root = rules / ".pending"
    root.mkdir(exist_ok=True)
    path = root / stage.name
    os.replace(stage, path)
    candidate = dict(
        revision=meta["revision"],
        base_revision=meta["base_revision"],
        time=meta["time"],
        counts=meta["counts"],
        changes=meta["changes"],
        declines=drops,
        expires_at=int(time.time()) + CANDIDATE_TTL,
        path=str(path.relative_to(rules)),
    )
    atomic(rules / ".candidate.json", json.dumps(candidate))
    for other in root.iterdir():
        if other != path and other.is_dir():
            shutil.rmtree(other, ignore_errors=True)


def previous_path(rules, current_revision):
    current = resolve_current(rules)
    record = read_json(current / "updated.json")
    if record.get("revision") != current_revision:
        return None
    name = record.get("previous_path", "")
    if name != "." and not re.fullmatch(r"\.generations/rules-[A-Za-z0-9_-]+", name):
        return None
    path = rules / name
    if path.is_symlink():
        return None
    if not path.is_dir() or not any((path / (kind + ".txt")).exists() for kind in KINDS):
        return None
    return path if directory_revision(path) != current_revision else None


def candidate_record(rules, current_revision, verify=False):
    record = read_json(rules / ".candidate.json")
    if record.get("base_revision") != current_revision or record.get("expires_at", 0) < time.time():
        return None
    if not re.fullmatch(r"\.pending/rules-[A-Za-z0-9_-]+", record.get("path", "")):
        return None
    path = rules / record["path"]
    if path.is_symlink():
        return None
    if not path.is_dir() or directory_revision(path, verify=verify) != record.get("revision"):
        return None
    return record


_EFFECTIVE_CACHE = {}


def effective_summary(snapshot, current, personal):
    from rules_overrides import effective_revision, revision as personal_revision, populated, merge

    key = (current["revision"], personal_revision(personal))
    if key in _EFFECTIVE_CACHE:
        return dict(_EFFECTIVE_CACHE[key])
    counts = dict(current["counts"])
    if populated(personal):
        for kind in KINDS:
            if personal[kind]["added"] or personal[kind]["excluded"]:
                counts[kind] = len(merge(lines(snapshot / (kind + ".txt")), kind, personal))
    result = dict(revision=effective_revision(current["revision"], personal), counts=counts)
    if len(_EFFECTIVE_CACHE) >= 8:
        _EFFECTIVE_CACHE.clear()
    _EFFECTIVE_CACHE[key] = result
    return dict(result)


def info():
    import rules_update as core

    with core.rule_snapshot() as snapshot:
        current = describe(snapshot)
        from rules_overrides import load, describe as describe_personal

        personal = load(core.RULES)
        effective = effective_summary(snapshot, current, personal)
        previous = previous_path(core.RULES, current["revision"])
        candidate = candidate_record(core.RULES, current["revision"])
        status = read_json(core.RUN / "status.json")
        fresh = 0 <= time.time() - status.get("time", 0) < 15
        state = "unknown"
        if fresh:
            state = (
                "inactive"
                if status.get("state") == "disabled"
                else "applied"
                if status.get("state") == "running"
                and status.get("rules_revision") == effective["revision"]
                else "pending"
            )
        result = dict(
            current=current,
            effective=effective,
            overrides=describe_personal(personal),
            previous=describe(previous) if previous else None,
            candidate={key: value for key, value in candidate.items() if key != "path"}
            if candidate
            else None,
            application=dict(
                state=state,
                revision=status.get("rules_revision", ""),
                status_time=status.get("time", 0),
            ),
            categories=[dict(id=key, label=value) for key, value in KINDS.items()],
        )
    try:
        from rules_schedule import Schedule

        result["schedule"] = Schedule(rules=core.RULES, run=core.RUN).plan(
            core.read_config()["main"]
        )
        if result["schedule"]["state"] == "awaiting_confirmation" and not result["candidate"]:
            result["schedule"]["message"] = (
                "异常候选已过期或失效；保留当前规则，请立即更新规则重新生成候选"
            )
    except ImportError:
        result["schedule"] = {}
    try:
        from rules_sources import presets

        result["source_presets"] = presets()
    except ImportError:
        result["source_presets"] = []
    return result


def query(request_id):
    import rules_update as core

    with claim_request(core.RUN / "rules-input", request_id, 4096) as payload:
        if not isinstance(payload, dict) or set(payload) - {
            "kind",
            "view",
            "search",
            "page",
            "page_size",
            "revision",
            "overrides_revision",
        }:
            raise ControlError("规则查询参数无效")
        kind, view = payload.get("kind", "china"), payload.get("view", "current")
        search = payload.get("search", "")
        page, size = payload.get("page", 1), payload.get("page_size", 100)
        if (
            kind not in KINDS
            or view
            not in (
                "current",
                "effective",
                "personal_added",
                "personal_excluded",
                "added",
                "removed",
                "candidate",
                "candidate_added",
                "candidate_removed",
            )
            or not isinstance(search, str)
            or len(search) > 253
            or type(page) is not int
            or type(size) is not int
            or not 1 <= page <= 100000
            or not 1 <= size <= 100
        ):
            raise ControlError("规则查询参数无效")
        with core.rule_snapshot() as snapshot:
            current_revision = directory_revision(snapshot)
            if payload.get("revision") != current_revision:
                raise ControlError("规则版本已变化，请刷新后重新查询")
            from rules_overrides import load, revision as personal_revision, merge

            personal = load(core.RULES)
            overrides_revision = personal_revision(personal)
            if (
                "overrides_revision" in payload
                or view in ("effective", "personal_added", "personal_excluded")
            ) and payload.get("overrides_revision") != overrides_revision:
                raise ControlError("个人规则已变化，请刷新后重新查询")
            candidate = None
            target = snapshot
            requested_view = view
            if view.startswith("candidate"):
                candidate = candidate_record(core.RULES, current_revision)
                if not candidate:
                    raise ControlError("待确认规则已变化或过期，请刷新")
                target = core.RULES / candidate["path"]
                requested_view = (
                    view.removeprefix("candidate_") if view != "candidate" else "current"
                )
            if view == "effective":
                values = merge(lines(snapshot / (kind + ".txt")), kind, personal)
            elif view in ("personal_added", "personal_excluded"):
                values = list(personal[kind]["added" if view == "personal_added" else "excluded"])
            else:
                values = (
                    lines(target / (kind + ".txt"))
                    if requested_view == "current"
                    else lines(target / ".diff" / (kind + "-" + requested_view))
                )
            if search:
                values = [value for value in values if search.lower() in value.lower()]
            total = len(values)
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            return dict(
                request_id=request_id.split(":")[0],
                revision=current_revision,
                kind=kind,
                view=view,
                search=search,
                page=page,
                page_size=size,
                total=total,
                pages=pages,
                entries=values[(page - 1) * size : page * size],
                candidate_revision=candidate["revision"] if candidate else "",
                overrides_revision=overrides_revision,
                origins={
                    value: "excluded"
                    if view == "personal_excluded"
                    or view == "current"
                    and value in personal[kind]["excluded"]
                    else "personal"
                    if value in personal[kind]["added"]
                    else "subscription"
                    for value in values[(page - 1) * size : page * size]
                },
            )


def mutate(request_id, action):
    import rules_update as core

    fields = {
        "revision",
        "confirm",
        "candidate_revision" if action == "accept" else "previous_revision",
    }
    with claim_request(core.RUN / "rules-input", request_id, 4096) as payload:
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or payload.get("confirm") is not True
        ):
            raise ControlError("请明确确认规则操作")
        with operation_lock(core.RULES):
            with core.rule_snapshot() as snapshot:
                current = describe(snapshot)
                if payload.get("revision") != current["revision"]:
                    raise ControlError("规则版本已变化，本次操作未执行，请刷新")
                if action == "accept":
                    candidate = candidate_record(core.RULES, current["revision"], verify=True)
                    if not candidate or candidate["revision"] != payload.get("candidate_revision"):
                        raise ControlError("待确认规则已变化或过期，请重新更新")
                    target = core.RULES / candidate["path"]
                else:
                    target = previous_path(core.RULES, current["revision"])
                    if target is None or directory_revision(target, verify=True) != payload.get(
                        "previous_revision"
                    ):
                        raise ControlError("上一版规则已变化或不可用，请刷新")
                pending = [
                    (
                        core.RULES / (kind + ".txt"),
                        "\n".join(lines(target / (kind + ".txt"))) + "\n",
                    )
                    for kind in KINDS
                    if (target / (kind + ".txt")).exists()
                ]
                if not pending:
                    raise ControlError("目标规则为空，本次操作未执行")
                metadata = read_json(target / "updated.json")
                latest_cfg = core.read_config()
                if action == "accept" and metadata.get(
                    "sources_fingerprint"
                ) != sources_fingerprint(latest_cfg["main"]):
                    raise ControlError("规则来源设置已变化，请重新更新后确认")
                from rules_overrides import load as load_personal, validate_required

                validate_required(latest_cfg, content(target), load_personal(core.RULES))
            # No nested snapshot lock during exclusive publication.
            meta = core.commit_rules(
                pending,
                dict(
                    time=int(time.time()),
                    downloaded_at=metadata.get("downloaded_at", metadata.get("time", 0)),
                    sources=metadata.get("sources", {}),
                    ignored_regexp=metadata.get("ignored_regexp", {}),
                    operation=action,
                ),
                allow_decline=True,
                replace_all=True,
            )
            if action == "accept":
                try:
                    (core.RULES / ".candidate.json").unlink(missing_ok=True)
                except OSError:
                    pass  # Base-revision binding already makes the stale offer unusable.
            from rules_schedule import Schedule

            schedule_warning = False
            try:
                Schedule(rules=core.RULES, run=core.RUN).resolve()
            except OSError:
                schedule_warning = True
            result = dict(
                request_id=request_id.split(":")[0],
                revision=meta["revision"],
                message="规则已保存，等待运行中应用并确认"
                if core.read_config()["main"].get("enabled") == "1"
                else "规则已保存；主线路关闭，尚未运行生效",
            )
            if schedule_warning:
                result["message"] += "；更新计划状态未能保存，请检查本机存储"
            try:
                atomic(
                    core.RUN / "update.json",
                    json.dumps(
                        dict(
                            state="success",
                            request_id=result["request_id"],
                            started_at=meta["time"],
                            finished_at=meta["time"],
                            last_success_at=Schedule(rules=core.RULES, run=core.RUN).plan(
                                core.read_config()["main"]
                            )["last_success_at"],
                            phase=result["message"],
                            percent=100,
                            counts=meta["counts"],
                            last_saved_at=meta["time"],
                        )
                    ),
                    0o644,
                )
            except OSError:
                result["message"] += "；反馈记录未保存，请读取当前规则版本确认"
            return result


def accept(request_id):
    return mutate(request_id, "accept")


def rollback(request_id):
    return mutate(request_id, "rollback")


def edit(request_id):
    """Change only persistent personal policy; never rewrite downloaded rule files."""
    import rules_update as core
    from rules_overrides import (
        entry,
        load,
        normalize,
        save,
        revision as personal_revision,
        effective_revision,
    )

    with claim_request(core.RUN / "rules-input", request_id, 4096) as payload:
        fields = {"kind", "action", "value", "revision", "overrides_revision"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ControlError("个人规则操作参数无效")
        kind, action = payload.get("kind"), payload.get("action")
        if kind not in KINDS or action not in ("add", "exclude", "restore", "remove_added"):
            raise ControlError("个人规则操作无效")
        value = entry(kind, payload.get("value"))
        with operation_lock(core.RULES):
            with (core.RULES / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                snapshot = resolve_current(core.RULES)
                source_revision = directory_revision(snapshot)
                personal = load(core.RULES)
                if payload.get("revision") != source_revision or payload.get(
                    "overrides_revision"
                ) != personal_revision(personal):
                    raise ControlError("订阅或个人规则已变化，本次修改未执行，请刷新")
                changed = normalize(personal)
                row = changed[kind]
                added, excluded = set(row["added"]), set(row["excluded"])
                if action == "add":
                    added.add(value)
                    excluded.discard(value)
                elif action == "exclude":
                    added.discard(value)
                    excluded.add(value)
                elif action == "restore":
                    excluded.discard(value)
                else:
                    added.discard(value)
                row.update(added=sorted(added), excluded=sorted(excluded))
                changed = normalize(changed)
                cfg = core.read_config()
                modes = (
                    [cfg["main"].get("lan_mode", "bypass_cn")]
                    if cfg["main"].get("scope") == "lan"
                    else [
                        p.get("mode", "bypass_cn")
                        for p in cfg.get("policies", [])
                        if p.get("enabled") == "1"
                    ]
                )
                needed = {"bypass_cn": "china", "gfw": "gfw", "whitelist": "whitelist"}
                if cfg["main"].get("enabled") == "1" and kind in {
                    needed.get(mode) for mode in modes
                }:
                    from rules_overrides import merge

                    if not merge(lines(snapshot / (kind + ".txt")), kind, changed):
                        raise ControlError(
                            "当前分流模式仍需要该分类规则，不能清空；请先调整分流模式"
                        )
                save(core.RULES, changed)
                return dict(
                    request_id=request_id.split(":")[0],
                    revision=source_revision,
                    overrides_revision=personal_revision(changed),
                    effective_revision=effective_revision(source_revision, changed),
                    message="个人规则已保存，等待运行中应用；后续订阅更新会继续保留"
                    if cfg["main"].get("enabled") == "1"
                    else "个人规则已保存；主线路关闭，开启后应用，订阅更新会继续保留",
                )
