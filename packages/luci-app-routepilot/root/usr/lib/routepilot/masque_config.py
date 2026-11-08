"""Compile device-only native routing around one official MASQUE client."""
import ipaddress
import re
import uuid
from compiler import read_rules, PRIVATE4, DOMAIN, MAC, ID

MARK = 0x5a000000
TABLE = 51990
PRIORITY = 11790


def normalize_domains(text):
    if len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError('域名名单超过 4 MB')
    result, ignored = [], 0
    for raw in text.splitlines():
        value = raw.strip().lower()
        if not value or value.startswith('#'):
            continue
        if value.startswith('regexp:'):
            # Never evaluate network-supplied regular expressions.
            ignored += 1
            continue
        prefix = 'full:' if value.startswith('full:') else ''
        value = value.removeprefix('full:').removeprefix('domain:').rstrip('.')
        if not DOMAIN.fullmatch(value):
            raise ValueError('域名规则格式错误：仅支持普通域名、domain: 与 full:')
        result.append(prefix + value)
        if len(result) > 200000:
            raise ValueError('域名规则超过 20 万条')
    if not result:
        raise ValueError('域名规则为空')
    return '\n'.join(sorted(set(result))) + '\n', ignored


def domain_file(directory, name):
    from pathlib import Path
    path = Path(directory) / name
    if not path.exists():
        return []
    return normalize_domains(path.read_text())[0].splitlines()


def values(main, key, ip=False):
    raw = main.get(key, [])
    raw = re.split(r'[,\s]+', raw.strip()) if isinstance(raw, str) else raw
    if not isinstance(raw, list) or len(raw) > 2000:
        raise ValueError('自定义规则格式或数量不正确')
    result = []
    for value in raw:
        if not value:
            continue
        if ip:
            net = ipaddress.ip_network(value, strict=False)
            if net.version != 4:
                raise ValueError('当前仅支持 IPv4 规则')
            result.append(str(net))
        else:
            value = value.lower().rstrip('.')
            if not DOMAIN.fullmatch(value) or '.' not in value:
                raise ValueError('请输入普通域名，不含网址前缀或通配符')
            result.append(value)
    return sorted(set(result))


def selected_lans(main):
    raw = main.get('lan_interfaces', [main.get('lan_interface', 'br-lan')])
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8 or len(set(raw)) != len(raw):
        raise ValueError('请选择 1–8 个不同 LAN 接口')
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', v) for v in raw):
        raise ValueError('LAN 接口名称无效')
    return raw


def resolve_exit(main):
    """Preserve all legacy mappings; new selections store the actual UUID."""
    selected = main.get('exit', 'seoul')
    raw = main.get('vnet_' + selected, '') if selected in ('default', 'seoul', 'usa') else selected
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('出口网络 ID 无效，请读取账户出口并明确选择；未回退到默认出口') from None


def compile_config(cfg, directory):
    main = cfg['main']
    from warp_control import preference
    preference(main.get('warp_protocol','auto'))
    backend = main.get('backend', 'masque')
    if backend not in ('masque', 'worker'):
        raise ValueError('线路类型无效')
    exit_name = 'worker' if backend == 'worker' else main.get('exit', 'seoul')
    vnet = '' if backend == 'worker' else resolve_exit(main)
    interfaces = selected_lans(main)
    lan = interfaces[0]
    dns = {}
    for key, default in (('dns_foreign', '1.1.1.1'), ('dns_domestic', '223.5.5.5')):
        address = ipaddress.IPv4Address(main.get(key, default))
        if not address.is_global:
            raise ValueError('DNS 必须是公网 IPv4 地址，避免解析循环')
        dns[key] = str(address)
    transport = main.get('dns_transport', 'tcp')
    if transport not in ('tcp', 'udp'):
        raise ValueError('DNS 传输方式无效')
    custom = {key: values(main, key, key.endswith('_ips')) for key in
              ('custom_direct_domains', 'custom_proxy_domains', 'custom_direct_ips', 'custom_proxy_ips')}
    china = read_rules(directory, 'china.txt', 'cidr')
    domain_rules = {name: domain_file(directory, filename)
                    for name, filename in (('gfw', 'gfw.txt'), ('whitelist', 'whitelist.txt'), ('china', 'china_domains.txt'))}
    if 'rule_overrides' in cfg:
        from rules_overrides import normalize, merge
        personal = normalize(cfg['rule_overrides'])
        china = merge(china, 'china', personal)
        domain_rules = {name: merge(values, {'china': 'china_domains'}.get(name, name), personal) for name, values in domain_rules.items()}
    scope = main.get('scope', 'devices')
    if scope not in ('devices', 'lan'):
        raise ValueError('接管范围无效')
    default_ipv6 = main.get('ipv6_mode', 'block')
    if default_ipv6 not in ('block', 'direct'):
        raise ValueError('IPv6 处理方式无效')
    policies = [p for p in cfg['policies'] if p.get('enabled') == '1']
    networks = main.get('lan_networks')
    if networks is None:
        networks = [{'interface': lan, 'network': main.get('lan_network', '')}]
    resolved = []
    if scope == 'lan' or main.get('lan_networks') is not None:
        if not isinstance(networks, list) or len(networks) != len(interfaces):
            raise ValueError('所选 LAN 接口尚未全部检测到网段')
        for index, item in enumerate(networks):
            if item.get('interface') != interfaces[index]:
                raise ValueError('LAN 接口与网段不匹配')
            try:
                network = ipaddress.IPv4Network(item.get('network', ''), strict=True)
            except ValueError:
                raise ValueError('尚未检测到 LAN 网段') from None
            if not 16 <= network.prefixlen <= 30 or not any(network.subnet_of(ipaddress.ip_network(n)) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) or network.overlaps(ipaddress.ip_network('10.254.239.0/30')):
                raise ValueError('LAN 网段不受支持或与隧道网段重叠')
            if any(network.overlaps(n) for _, n in resolved):
                raise ValueError('所选 LAN 网段重叠，未应用新策略')
            resolved.append((item['interface'], network))
    if scope == 'lan':
        policies = [dict(id='lan' if i == 0 else 'lan' + str(i), enabled='1', ip=str(network), lan_interface=iface,
                        mode=main.get('lan_mode', 'bypass_cn'), scope='lan') for i, (iface, network) in enumerate(resolved)]
    if not policies or len(policies) > 8:
        raise ValueError('请启用 1–8 台设备的策略')
    devices, dns_policies, sets, capture, ipv6, redirect = [], [], [], [], [], []
    ips, macs = set(), set()
    private = '{ ' + ', '.join(PRIVATE4) + ' }'
    mark = 'counter meta mark set (meta mark & 0x00ffffff) | 0x5a000000 return'
    def rule(match, condition, proxy):
        return '    ' + match + ' ' + condition + ' ' + (mark if proxy else 'return')
    for index, policy in enumerate(policies):
        ident = str(policy.get('id', ''))
        mac = str(policy.get('mac', '')).lower()
        whole_lan = policy.get('scope') == 'lan'
        ip = str(ipaddress.IPv4Network(policy['ip'])) if whole_lan else str(ipaddress.IPv4Address(policy.get('ip', '')))
        if not ID.fullmatch(ident) or len(ident) > 32 or (not whole_lan and (not MAC.fullmatch(mac) or int(mac[:2], 16) & 1)):
            raise ValueError('设备标识或 MAC 不正确')
        if not whole_lan and not any(ipaddress.ip_address(ip) in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('设备必须使用局域网 IPv4 地址')
        if ip in ips or (not whole_lan and mac in macs):
            raise ValueError('同一设备只能启用一条策略')
        ips.add(ip); macs.add(mac)
        mode = policy.get('mode', 'bypass_cn')
        if mode not in ('direct', 'global', 'bypass_cn', 'gfw', 'whitelist'):
            raise ValueError('分流模式无效')
        if whole_lan:
            lan = policy['lan_interface']
        elif resolved:
            matches = [iface for iface, subnet in resolved if ipaddress.ip_address(ip) in subnet]
            if len(matches) != 1:
                raise ValueError('所选设备不在已选择的 LAN 网段中')
            lan = matches[0]
        ipv6_choice = policy.get('ipv6_mode', 'inherit')
        if ipv6_choice not in ('inherit', 'block', 'direct'):
            raise ValueError('设备 IPv6 处理方式无效')
        effective_ipv6 = default_ipv6 if ipv6_choice == 'inherit' else ipv6_choice
        devices.append({'id': ident, 'mac': mac, 'ip': ip, 'mode': mode, 'scope': 'lan' if whole_lan else 'device', 'lan_interface': lan, 'ipv6_mode': effective_ipv6 if mode != 'direct' else 'unmanaged'})
        if mode == 'direct':
            continue
        if mode == 'bypass_cn' and not china:
            raise ValueError('国内直连模式需要有效的中国 IP 规则')
        if mode in ('gfw', 'whitelist') and not domain_rules[mode]:
            raise ValueError('所选域名分流模式需要先更新相应规则')
        direct_set, proxy_set, mode_set = ('rpd_' + ident, 'rpp_' + ident, 'rpg_' + ident)
        for name in (direct_set, proxy_set, mode_set):
            sets.append('  set %s { type ipv4_addr; flags timeout; timeout 1h; }' % name)
        match = ('iifname "%s" ip saddr %s' % (lan, ip)) if whole_lan else ('iifname "%s" ether saddr %s ip saddr %s' % (lan, mac, ip))
        port = 12100 + index
        redirect.append('    %s meta l4proto { tcp, udp } th dport 53 counter redirect to :%d' % (match, port))
        capture.append(rule(match, 'ip daddr ' + private, False))
        if whole_lan and main.get('bypass_clients', '1') == '1':
            capture.append(rule(match, 'ip daddr { 162.159.192.0/24, 162.159.193.0/24, 162.159.197.0/24 }', False))
        for field, condition in (('custom_direct_ips', 'ip daddr'), ('custom_proxy_ips', 'ip daddr')):
            if custom[field]:
                capture.append(rule(match, condition + ' { ' + ', '.join(custom[field]) + ' }', 'proxy' in field))
        capture.append(rule(match, 'ip daddr @' + direct_set, False))
        capture.append(rule(match, 'ip daddr @' + proxy_set, True))
        if mode == 'bypass_cn':
            capture.append(rule(match, 'ip daddr @china4', False))
        elif mode in ('gfw', 'whitelist'):
            capture.append(rule(match, 'ip daddr @' + mode_set, mode == 'gfw'))
        capture.append(rule(match, '', mode != 'gfw'))
        if effective_ipv6 == 'block':
            ipv6.append(('    iifname "%s" meta nfproto ipv6 reject with icmpv6 type admin-prohibited' % lan) if whole_lan else ('    iifname "%s" ether saddr %s meta nfproto ipv6 reject with icmpv6 type admin-prohibited' % (lan, mac)))
        dns_policies.append(dict(port=port, label=lan if whole_lan else str(policy.get('name') or ident)[:64], mark=MARK, mode=mode, ip=ip, mac=mac, block_aaaa=effective_ipv6 == 'block',
             proxy_set=proxy_set, direct_set=direct_set, mode_set=mode_set,
             mode_domains=domain_rules.get(mode, []), china_domains=domain_rules['china'],
             custom_direct_domains=custom['custom_direct_domains'], custom_proxy_domains=custom['custom_proxy_domains']))
    china_set = '  set china4 { type ipv4_addr; flags interval; auto-merge;'
    if china:
        china_set += ' elements = { ' + ', '.join(china) + ' };'
    china_set += ' }'
    nft = '\n'.join(['table inet routepilot {', china_set, *sets,
        '  chain dns_redirect { type nat hook prerouting priority -110; policy accept;', *redirect, '  }',
        '  chain device_route { type filter hook prerouting priority -90; policy accept;', *capture, '  }',
        '  chain ipv6_guard { type filter hook forward priority -10; policy accept;', *ipv6, '  }',
        '  chain dns_guard { type filter hook input priority -10; policy accept;',
        '    iifname "lo" accept',
        '    meta l4proto { tcp, udp } th dport 12100-12107 ct status dnat accept',
        '    meta l4proto { tcp, udp } th dport 12100-12107 reject',
        '  }', '}', ''])
    return dict(nft=nft, devices=devices, vnet=vnet, exit=exit_name, scope=scope,
        dns=dict(**dns, transport=transport, cache=main.get('dns_cache', '1') == '1',
                 block_aaaa=True, policies=dns_policies))
