"""Independent Cloudflare client enrollment adapter. Never imports the router plane.

The enrollment protocol is based on the public usque implementation (MIT), not a
Cloudflare-supported management API. No client daemon or route changes are used.
"""
import base64
import codecs
import datetime
import ipaddress
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = 'https://api.cloudflareclient.com/v0a4471'
MAX_REPLY = 262144
PORTS = (443, 500, 1701, 4500, 4443, 8443, 8095)
P256_PREFIX = bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d03010703420004')


class ClientError(ValueError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def organization(value):
    if not isinstance(value, str):
        raise ClientError('请填写团队名称')
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise ClientError('团队名称只填写名称，不填写网址或邮箱')
    return value


def team_token(value, org, now):
    if not isinstance(value, str) or len(value) > 15000:
        raise ClientError('授权链接无效')
    value = value.strip()
    if value.startswith('com.cloudflare.warp://'):
        url = urllib.parse.urlsplit(value)
        if url.netloc != org + '.cloudflareaccess.com' or url.path != '/auth' or url.fragment:
            raise ClientError('授权链接与此客户端的团队不一致')
        query = urllib.parse.parse_qs(url.query, strict_parsing=True)
        if set(query) != {'token'} or len(query['token']) != 1:
            raise ClientError('授权链接格式无效')
        value = query['token'][0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', value):
        raise ClientError('请粘贴官方登录完成后的授权链接或团队 JWT')
    try:
        claims = json.loads(base64.urlsafe_b64decode(value.split('.')[1] + '==='))
        expiry = claims['exp']
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= now:
            raise ValueError()
        issuer = claims.get('iss')
        if issuer and (not isinstance(issuer, str) or issuer.rstrip('/') != 'https://' + org + '.cloudflareaccess.com'):
            raise ClientError('授权令牌的团队与当前客户端不一致')
    except ClientError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ClientError('授权令牌已过期或内容无效，请重新登录') from None
    # This is only an early format/expiry check. Cloudflare verifies the signature.
    return value


def openssl(args, data=None):
    try:
        result = subprocess.run(['/usr/bin/openssl'] + args, input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError('本机缺少可用的 OpenSSL；未提交云端注册') from None
    if result.returncode:
        raise ClientError('本机密钥生成或校验失败；请核对 OpenSSL 支持')
    return result.stdout


def new_keys():
    pem = openssl(['ecparam', '-name', 'prime256v1', '-genkey', '-noout'])
    private = openssl(['pkcs8', '-topk8', '-nocrypt', '-outform', 'DER'], pem)
    public = openssl(['pkey', '-pubout', '-outform', 'DER'], pem)
    # Same opaque 32-byte bootstrap value as usque GenerateRandomWgPubkey.
    # It is never used to connect; the fresh P-256 key is the actual identity.
    bootstrap_public = os.urandom(32)
    if len(public) != 91 or not public.startswith(P256_PREFIX) or len(bootstrap_public) != 32:
        raise ClientError('本机返回了不支持的密钥格式')
    return dict(private_key=base64.b64encode(private).decode(),
                public_key=base64.b64encode(public).decode(),
                bootstrap_key=base64.b64encode(bootstrap_public).decode())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, path, *, token=None, jwt=None, body=None):
    if not re.fullmatch(r'/reg(?:/(?:t\.)?[a-f0-9-]{36})?', path):
        raise ClientError('注册接口路径无效')
    if path != '/reg' and registration_id(path[5:]) != path[5:]:
        raise ClientError('注册接口路径无效')
    # OpenWrt python3-light omits IDNA. Check before any network write so a
    # missing optional codec cannot become an ambiguous registration result.
    try:
        codecs.lookup('idna')
    except (LookupError, ImportError):
        raise ClientError('缺少 Python 域名编码依赖（python3-codecs）；未提交云端请求') from None
    headers = {'Content-Type': 'application/json; charset=UTF-8',
               'User-Agent': 'WARP for Android', 'CF-Client-Version': 'a-6.35-4471'}
    if token:
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]{1,4096}={0,2}', token):
            raise ClientError('设备授权凭据格式无效')
        headers['Authorization'] = 'Bearer ' + token
    if jwt:
        headers['CF-Access-Jwt-Assertion'] = jwt
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=12) as response:
            status = response.status
            if status not in (200, 201, 204) or method == 'DELETE' and status not in (200, 204):
                raise ClientError('云端尚未确认操作完成，请刷新核对', uncertain=method != 'GET')
            raw = response.read(MAX_REPLY + 1)
            if len(raw) > MAX_REPLY:
                raise ClientError('云端返回内容过大，请核对注册状态', uncertain=method != 'GET')
            if method == 'DELETE' and not raw.strip():
                return {'deleted': True}
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            if result.get('success') is False or result.get('errors'):
                raise ClientError('Cloudflare 未确认操作成功，请刷新核对', uncertain=method != 'GET')
            if method == 'DELETE':
                if (result.get('success') is not True and result.get('deleted') is not True or
                        result.get('deleted') is False or result.get('status') in ('pending', 'accepted', 'queued', 'processing')):
                    raise ClientError('云端撤销结果未确认，保留本地凭据', uncertain=True)
                return {'deleted': True}
            return result
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == 'DELETE':
            # A missing protocol version or route can also return 404. It is
            # not authoritative proof that this device was revoked.
            raise ClientError('云端返回未找到，撤销尚未确认；保留本地凭据并核对接口或设备状态', uncertain=True) from None
        messages = {401: '设备授权或登录令牌已失效', 403: '入网权限不足或组织策略不支持第三方客户端',
                    429: 'Cloudflare 请求受限，请稍后再试'}
        raise ClientError(messages.get(error.code, 'Cloudflare 请求失败（HTTP %s）' % error.code),
                          uncertain=method != 'GET' and (error.code < 400 or error.code >= 500 or error.code in (408, 409))) from None
    except ClientError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise ClientError('云端响应未能确认；请刷新核对，不要重复注册', uncertain=method != 'GET') from None


def register(jwt, bootstrap_key, serial):
    return request('POST', '/reg', jwt=jwt, body={
        'key': bootstrap_key, 'install_id': '', 'fcm_token': '',
        'tos': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'model': 'PC', 'serial_number': str(uuid.UUID(serial)), 'os_version': '',
        'key_type': 'curve25519', 'tunnel_type': 'wireguard', 'locale': 'en_US'})


def enroll(ident, token, public_key, name):
    return request('PATCH', '/reg/' + ident, token=token,
                   body={'key': public_key, 'key_type': 'secp256r1', 'tunnel_type': 'masque', 'name': name})


def registration_id(value):
    if not isinstance(value, str):
        raise ClientError('设备注册 ID 格式无效')
    prefix = 't.' if value.startswith('t.') else ''
    body = value[len(prefix):]
    if not re.fullmatch(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}', body):
        raise ClientError('设备注册 ID 格式无效')
    return prefix + str(uuid.UUID(body))


def registration_key(value):
    ident = registration_id(value)
    return ident[2:] if ident.startswith('t.') else ident


def response_identity(reply):
    try:
        ident = registration_id(reply['id'])
        token = reply['token']
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]{1,4096}={0,2}', token):
            raise ValueError()
        return ident, token
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ClientError('注册返回缺少可核验的设备身份，云端结果未知', uncertain=True) from None


def check_team(reply, org):
    account = reply.get('account', {})
    actual = ''
    if isinstance(account, dict):
        try:
            actual = organization(account.get('organization'))
        except ClientError:
            pass
    if not isinstance(account, dict) or account.get('account_type') != 'team' or actual != org:
        raise ClientError('返回的注册不属于指定团队；未生成导出，请核对后撤销此新增注册')



def registered_config(reply, org, ident, expected_key):
    try:
        if registration_id(reply['id']) != registration_id(ident):
            raise ValueError()
        check_team(reply, org)
        if reply.get('revoked_at') or reply.get('deleted_at'):
            raise ClientError('云端记录已撤销或删除，不能生成导出')
        if reply.get('key_type') != 'secp256r1' or reply.get('tunnel_type') != 'masque' or reply.get('key') != expected_key:
            raise ClientError('云端 MASQUE 公钥尚未匹配，请刷新注册状态')
        cfg = reply['config']
        addresses = cfg['interface']['addresses']
        if not isinstance(addresses['v4'], str) or addresses.get('v6') is not None and not isinstance(addresses.get('v6'), str):
            raise ValueError()
        ipv4 = str(ipaddress.IPv4Address(addresses['v4']))
        if ipaddress.ip_address(ipv4).is_multicast or ipaddress.ip_address(ipv4).is_unspecified or ipaddress.ip_address(ipv4).is_loopback:
            raise ValueError()
        ipv6 = str(ipaddress.IPv6Address(addresses['v6'])) if addresses.get('v6') else ''
        if ipv6 and (ipaddress.ip_address(ipv6).is_unspecified or ipaddress.ip_address(ipv6).is_loopback or ipaddress.ip_address(ipv6).is_multicast or ipaddress.ip_address(ipv6).is_link_local):
            raise ValueError()
        peer = cfg['peers'][0]
        key = peer['public_key']
        key = ''.join(key.replace('-----BEGIN PUBLIC KEY-----', '').replace('-----END PUBLIC KEY-----', '').split())
        raw = base64.b64decode(key, validate=True)
        if len(raw) != 91 or not raw.startswith(P256_PREFIX):
            raise ValueError()
        endpoint = peer['endpoint']['v4']
        host, port = endpoint.rsplit(':', 1)
        if not re.fullmatch('[0-9]{1,5}', port) or int(port) > 65535 or ipaddress.IPv4Address(host) not in ipaddress.IPv4Network('162.159.197.0/24'):
            raise ValueError()
        return dict(ipv4=ipv4, ipv6=ipv6, peer_key=key, suggested_endpoint=host)
    except ClientError:
        raise
    except (KeyError, ValueError, IndexError, TypeError, AttributeError):
        raise ClientError('云端返回了不支持的 MASQUE 配置，未生成导出') from None


def network_id(value):
    """Canonical UUID only; never accept a URL, label or an implicit default."""
    if not isinstance(value, str):
        raise ClientError('出口标识无效，请刷新目录后重新选择')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise ClientError('出口标识无效，请刷新目录后重新选择') from None
    return value


def network_catalog(value):
    """Validate and reduce the cloud reply before persistence or public RPC."""
    try:
        selected = network_id(value['selected_id'])
        source = value['networks']
        if not isinstance(source, list) or not 1 <= len(source) <= 128:
            raise ValueError()
        networks, seen = [], set()
        for item in source:
            ident, name, default = network_id(item['id']), item['name'], item['is_default']
            if ident in seen or not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name) or type(default) is not bool:
                raise ValueError()
            seen.add(ident)
            networks.append(dict(id=ident, name=name.strip(), is_default=default))
        if selected not in seen:
            raise ValueError()
        return dict(selected_id=selected, networks=networks)
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ClientError('云端出口目录无效或当前出口已不在目录中；未回落到默认网络') from None


def client_networks(reply, org, ident, public_key, token, target=None):
    """Owned-registration VNET API, confirmed against native 2026.8.2115.0.

    GET reads its directory/selection. PATCH selects one UUID for this identity;
    it does not modify any network, route, account default or other registration.
    Callers validate the fresh directory and read back after every PATCH.
    """
    registered_config(reply, org, ident, public_key)
    ident = registration_id(ident)
    account = reply['account'].get('id')
    if not isinstance(account, str) or not re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})', account):
        raise ClientError('注册返回缺少可核验的团队账户；未提交出口设置')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]{1,4096}={0,2}', token):
        raise ClientError('设备授权凭据格式无效')
    path = '/v0/accounts/' + account + '/reg/' + ident + '/virtualnetworks'
    method = 'GET'
    if target is not None:
        path += '/' + network_id(target)
        method = 'PATCH'
    try:
        codecs.lookup('idna')
    except (LookupError, ImportError):
        raise ClientError('缺少 Python 域名编码依赖（python3-codecs）；未提交云端请求') from None
    req = urllib.request.Request('https://api.cloudflareclient.com' + path, method=method,
                                 headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'WARP for Android',
                                          'CF-Client-Version': 'a-6.35-4471', 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=12) as response:
            raw = response.read(MAX_REPLY + 1)
            if response.status != 200 or len(raw) > MAX_REPLY:
                raise ClientError('云端出口操作未能确认，请同步核对', uncertain=target is not None)
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('success') is not True or data.get('errors'):
                raise ClientError('Cloudflare 未确认出口操作，请同步核对', uncertain=target is not None)
            if target is not None:
                return {'acknowledged': True}
            result = data['result']
            return network_catalog(dict(selected_id=result['selected_virtual_network_id'],
                                        networks=[dict(id=n['id'], name=n['name'], is_default=n['is_default_network'])
                                                  for n in result['virtual_networks']]))
    except urllib.error.HTTPError as error:
        messages = {401: '客户端凭据已失效，请核对注册状态', 403: '此客户端没有出口选择权限',
                    404: '客户端出口接口或注册不存在；未使用默认网络替代', 429: '出口请求受限，请稍后同步'}
        raise ClientError(messages.get(error.code, '出口请求失败（HTTP %s）' % error.code),
                          uncertain=target is not None and (error.code >= 500 or error.code in (408, 409))) from None
    except ClientError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, urllib.error.URLError):
        raise ClientError('云端出口结果未能确认；请同步核对，不要重复应用', uncertain=target is not None) from None
