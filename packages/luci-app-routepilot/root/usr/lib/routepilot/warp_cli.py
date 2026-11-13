"""Parse the official WARP client's public command outputs."""

from enum import Enum
import re
import subprocess
import uuid

PROTOCOLS = ("MASQUE", "WireGuard")
MAX_NETWORKS = 512
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){2,5}(?:-[0-9]+)?")
# Only the version observed on the imOK test router is currently verified.
VERIFIED_VERSIONS = frozenset({"2026.8.2115.0"})
UNKNOWN_WARNING = "客户端状态无法识别，当前客户端版本可能不受支持"


class TunnelStatus(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    DISCONNECTING = "disconnecting"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class InvalidDirectory(ValueError):
    """The client directory could not establish which exit is selected."""


class UnknownTunnelStatus(RuntimeError):
    """Status text could not establish whether a requested change is safe."""

    def __init__(self):
        super().__init__(UNKNOWN_WARNING)


def parse_status(text):
    rows = re.findall(r"^Status update:[ \t]*([^\r\n]+)", text, re.M)
    if len(rows) != 1:
        return TunnelStatus.UNKNOWN
    try:
        state = TunnelStatus(rows[0].strip().lower())
    except ValueError:
        return TunnelStatus.UNKNOWN
    return state if state is not TunnelStatus.UNAVAILABLE else TunnelStatus.UNKNOWN


def read_status(cli, *, timeout=None):
    try:
        options = {"timeout": timeout} if timeout is not None else {}
        result = cli("status", check=False, **options)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return TunnelStatus.UNAVAILABLE
    state = parse_status(result.stdout)
    if getattr(result, "returncode", 0) and (
        state is not TunnelStatus.UNKNOWN or not result.stdout.strip()
    ):
        return TunnelStatus.UNAVAILABLE
    return state


def parse_version(text):
    match = re.search(r"^warp-cli[ \t]+(" + VERSION.pattern + r")[ \t]*$", text.strip(), re.M)
    return match[1] if match else None


class NetworkDirectory:
    __slots__ = ("selected", "networks")

    def __init__(self, selected, networks):
        self.selected = selected
        self.networks = networks


def parse_selected_network(text):
    first = text.lstrip().partition("\n")[0].rstrip("\r")
    match = re.fullmatch(r"Currently selected:[ \t]*([0-9a-fA-F-]{36})[ \t]*", first)
    if not match:
        return None
    try:
        return str(uuid.UUID(match[1]))
    except ValueError:
        return None


def parse_settings(text):
    match = re.search(r"^([^\n]*)WARP tunnel protocol:\s*(MASQUE|WireGuard)\s*$", text, re.M)
    if not match:
        return None
    mdm = "(mdm)" in match[1].lower()
    managed = "network policy" in match[1] or mdm
    return dict(
        protocol=match[2],
        managed=managed,
        source="mdm"
        if mdm
        else "organization"
        if managed
        else "local_policy"
        if "local policy" in match[1]
        else "local",
        local_switch_allowed=not managed,
        supported=list(PROTOCOLS),
    )


def parse_directory(text):
    if len(text.encode("utf-8")) > 1048576 or "\x1b" in text:
        raise InvalidDirectory("出口目录格式无效")
    prefix, separator, body = text.partition("Virtual Networks:")
    if not separator or not re.fullmatch(
        r"\s*Currently selected:\s*(?:[0-9a-fA-F-]{36}|None|none)?\s*", prefix
    ):
        raise InvalidDirectory("未能识别出口目录格式")
    blocks = re.split(r"^\s*ID:\s*", body, flags=re.M)
    if blocks[0].strip() or len(blocks) - 1 > MAX_NETWORKS:
        raise InvalidDirectory("出口目录格式无效或数量过多")
    items, seen = [], set()
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        try:
            ident = str(uuid.UUID(lines[0].strip()))
        except (ValueError, IndexError):
            raise InvalidDirectory("出口目录含无效网络 ID") from None
        fields = {}
        for line in lines[1:]:
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*(Name|Comment|Default):\s*(.*?)\s*", line)
            if not match or match[1] in fields:
                raise InvalidDirectory("出口目录格式已变化，请核对客户端版本")
            fields[match[1]] = match[2]
        name = fields.get("Name", "")
        if (
            not name
            or len(name) > 256
            or any(ord(c) < 32 for c in name)
            or fields.get("Default") not in ("true", "false")
            or ident in seen
        ):
            raise InvalidDirectory("出口目录内容无效或重复")
        seen.add(ident)
        items.append(dict(id=ident, name=name, is_default=fields["Default"] == "true"))
    return NetworkDirectory(parse_selected_network(prefix), items)
