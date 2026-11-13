"""Optional rule-source choices; never read or change the saved configuration.

URLs were checked against their owning projects and parsed in full with imok's
validators on 2026-11-08. Reachability and file contents can change afterwards.
Distribution mirrors share the upstream project and may serve an older cache.
No external code, binary geoip/geosite files or encoded GFWList is executed.
"""

from copy import deepcopy


_PRESETS = (
    dict(
        id="ipip-github",
        label="IPIP / 17mon · GitHub",
        kinds=["china"],
        url="https://raw.githubusercontent.com/17mon/china_ip_list/master/china_ip_list.txt",
        project="17mon/china_ip_list",
        mirror=False,
    ),
    dict(
        id="misaka-github",
        label="Misaka chnroutes2 · GitHub",
        kinds=["china"],
        url="https://raw.githubusercontent.com/misakaio/chnroutes2/master/chnroutes.txt",
        project="misakaio/chnroutes2",
        mirror=False,
    ),
    dict(
        id="gaoyifan-github",
        label="Gaoyifan 中国 IPv4 · GitHub",
        kinds=["china"],
        url="https://raw.githubusercontent.com/gaoyifan/china-operator-ip/ip-lists/china.txt",
        project="gaoyifan/china-operator-ip",
        mirror=False,
    ),
    dict(
        id="gaoyifan-pages",
        label="Gaoyifan 中国 IPv4 · 官方 Pages 分发镜像",
        kinds=["china"],
        url="https://gaoyifan.github.io/china-operator-ip/china.txt",
        project="gaoyifan/china-operator-ip",
        mirror=True,
    ),
    dict(
        id="loyal-direct-github",
        label="Loyalsoldier 直连域名 · GitHub",
        kinds=["china_domains", "whitelist"],
        url="https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/direct-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=False,
    ),
    dict(
        id="loyal-direct-jsdelivr",
        label="Loyalsoldier 直连域名 · jsDelivr 分发镜像（可能滞后）",
        kinds=["china_domains", "whitelist"],
        url="https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=True,
    ),
    dict(
        id="loyal-direct-fastly",
        label="Loyalsoldier 直连域名 · Fastly 分发镜像（可能滞后）",
        kinds=["china_domains", "whitelist"],
        url="https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=True,
    ),
    dict(
        id="loyal-proxy-github",
        label="Loyalsoldier 代理域名 · GitHub",
        kinds=["gfw"],
        url="https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/proxy-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=False,
    ),
    dict(
        id="loyal-proxy-jsdelivr",
        label="Loyalsoldier 代理域名 · jsDelivr 分发镜像（可能滞后）",
        kinds=["gfw"],
        url="https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/proxy-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=True,
    ),
    dict(
        id="loyal-proxy-fastly",
        label="Loyalsoldier 代理域名 · Fastly 分发镜像（可能滞后）",
        kinds=["gfw"],
        url="https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/proxy-list.txt",
        project="Loyalsoldier/v2ray-rules-dat",
        mirror=True,
    ),
)


def presets():
    """Return fresh catalog data; choosing or applying a source is a separate action."""
    return deepcopy(list(_PRESETS))
