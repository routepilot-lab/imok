"""Persistent, bounded personal additions/exclusions separate from subscriptions."""

import hashlib
import ipaddress
import json
import os
import stat
import time
from pathlib import Path

from compiler import DOMAIN
from public_errors import ControlError
from runtime import RULES, atomic

KINDS = ("china", "china_domains", "gfw", "whitelist")
MAX_ENTRIES = 10000
MAX_BYTES = 4 * 1024 * 1024


def empty():
    return {kind: dict(added=[], excluded=[]) for kind in KINDS}


def entry(kind, value):
    if (
        kind not in KINDS
        or not isinstance(value, str)
        or not 1 <= len(value) <= 260
        or any(c.isspace() or ord(c) < 32 for c in value.strip())
    ):
        raise ControlError("请输入一条有效域名或公网 IPv4/CIDR，不含网址、脚本或空白")
    value = value.strip().lower()
    if kind == "china":
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ControlError("中国 IP 条目仅接受公网 IPv4/CIDR") from None
        if network.version != 4 or network.prefixlen < 8 or not network.is_global:
            raise ControlError("中国 IP 条目仅接受公网 IPv4/CIDR，前缀至少 /8")
        return str(network)
    prefix = "full:" if value.startswith("full:") else ""
    value = value.removeprefix("full:").removeprefix("domain:").rstrip(".")
    if not DOMAIN.fullmatch(value):
        raise ControlError("域名条目仅支持普通域名、domain: 与 full:，不接受网址或正则")
    return prefix + value


def normalize(value):
    if not isinstance(value, dict) or set(value) - set(KINDS):
        raise ControlError("个人规则分类无效")
    result = empty()
    count = 0
    for kind in KINDS:
        row = value.get(kind, {})
        if not isinstance(row, dict) or set(row) - {"added", "excluded"}:
            raise ControlError("个人规则字段无效")
        for field in ("added", "excluded"):
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > MAX_ENTRIES:
                raise ControlError("个人规则数量无效")
            result[kind][field] = sorted(set(entry(kind, item) for item in values))
            count += len(result[kind][field])
        if set(result[kind]["added"]) & set(result[kind]["excluded"]):
            raise ControlError("同一条目不能同时新增和排除")
    if count > MAX_ENTRIES:
        raise ControlError("个人规则总数最多 10000 条")
    return result


def revision(rules):
    return hashlib.sha256(
        json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def path(rules=None):
    return (RULES if rules is None else Path(rules)).parent / "rule-overrides.json"


def load(rules=None):
    target = path(rules)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return empty()
    except OSError:
        raise ControlError("个人规则文件无法安全读取，请检查本机规则目录") from None
    try:
        with os.fdopen(fd, "r") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ControlError("个人规则文件类型或大小无效")
            document = json.load(handle)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "revision", "updated_at", "rules"}
            or document.get("version") != 1
        ):
            raise ControlError("个人规则文件格式无效")
        result = normalize(document["rules"])
        if document.get("revision") != revision(result):
            raise ControlError("个人规则完整性校验失败，请检查本机备份")
        return result
    except (ValueError, UnicodeError, TypeError):
        raise ControlError("个人规则文件无法识别，请检查本机备份") from None


def save(rules_directory, value):
    value = normalize(value)
    document = dict(version=1, revision=revision(value), updated_at=int(time.time()), rules=value)
    atomic(path(rules_directory), json.dumps(document, ensure_ascii=False))
    return value


def populated(rules):
    return any(rules[kind][field] for kind in KINDS for field in ("added", "excluded"))


def effective_revision(source_revision, rules):
    if not populated(rules):
        return source_revision
    return hashlib.sha256((source_revision + "\0" + revision(rules)).encode()).hexdigest()


def merge(values, kind, rules):
    row = rules[kind]
    return sorted((set(values) - set(row["excluded"])) | set(row["added"]))


def describe(rules):
    return dict(
        revision=revision(rules),
        counts={
            kind: {field: len(rules[kind][field]) for field in ("added", "excluded")}
            for kind in KINDS
        },
        total=sum(len(rules[k][f]) for k in KINDS for f in ("added", "excluded")),
    )


def validate_required(cfg, rows, rules):
    """Publishing subscriptions must not empty an active mode after exclusions."""
    main = cfg["main"]
    if main.get("enabled") != "1":
        return
    modes = (
        [main.get("lan_mode", "bypass_cn")]
        if main.get("scope") == "lan"
        else [
            p.get("mode", "bypass_cn") for p in cfg.get("policies", []) if p.get("enabled") == "1"
        ]
    )
    required = {"bypass_cn": "china", "gfw": "gfw", "whitelist": "whitelist"}
    for kind in {required[mode] for mode in modes if mode in required}:
        if not merge(rows.get(kind, []), kind, rules):
            raise ControlError(
                "个人覆盖后，当前分流模式需要的规则为空；保留现有规则，请先核对个人排除项或分流模式"
            )
