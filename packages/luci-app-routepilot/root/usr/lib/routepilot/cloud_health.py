"""Bounded deployment diagnostics; never echo a remote body, stack or credential."""

import json
import re
import subprocess


def api_hint(code):
    code = code if type(code) is int else None
    hints = {
        10021: (
            "模板校验失败（10021）：检查语法、初始化耗时与运行时兼容；保留已验证模板，在同一项目修正后重试",
            "template",
        ),
        10069: ("模板含不支持的事件处理器（10069）；请使用对应平台的部署格式", "template"),
    }
    return hints.get(
        code,
        (
            "Cloudflare 操作失败"
            + ("（错误码 " + str(code) + "）" if type(code) is int else "")
            + "；核对账户、权限和资源状态",
            "api",
        ),
    )


def probe(hostname, run=subprocess.run):
    r = run(
        [
            "curl",
            "-q",
            "-4",
            "-sS",
            "--proxy",
            "",
            "--noproxy",
            "*",
            "--proto",
            "=https",
            "--connect-timeout",
            "4",
            "--max-time",
            "8",
            "--max-filesize",
            "8192",
            "-w",
            "\n%{http_code}",
            "https://" + hostname + "/health",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    body, _, code = r.stdout.rpartition(b"\n")
    http = int(code) if code.isdigit() else 0
    if r.returncode:
        category = (
            "dns" if r.returncode == 6 else "tls" if r.returncode in (35, 51, 60) else "network"
        )
        return dict(
            state="waiting_dns" if category in ("dns", "tls") else "unreachable",
            health_category=category,
            health_curl=r.returncode,
            message={
                "dns": "域名尚不能解析；检查绑定与 DNS 生效",
                "tls": "HTTPS 证书尚未验证通过；检查证书状态，不关闭证书验证",
                "network": "健康检查连接未完成；保留同一项目稍后重试",
            }[category],
        )
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if (
        http == 200
        and isinstance(data, dict)
        and data.get("service") == "routepilot-worker"
        and data.get("build") == "cmliu-a8ab111-ordered-relays-v2"
    ):
        return dict(
            state="ready",
            health_category="ready",
            message="域名和已验证模板 HTTPS 就绪；设为备用后仍需独立联网检测",
        )
    match = re.search(rb"\b(1101|1102|1103|1019|1021|1024|1027)\b", body) if http >= 400 else None
    error = int(match[1]) if match else None
    hints = {
        1101: "Worker 运行异常：查看本项目日志并检查代码接线",
        1102: "Worker 超过 CPU 限额：检查耗时与账户额度",
        1103: "平台要求账户所有者联系 Cloudflare 支持；不会自动换账户或反复创建项目",
        1027: "请求额度已用尽：查看账户额度",
        1019: "请求循环次数超限：检查路由和自身请求循环",
        1021: "Worker 访问了不允许的目标",
        1024: "目标受到 Cloudflare 出站连接限制：检查公共转发候选",
    }
    return dict(
        state="deployment_error",
        health_category="platform" if error else "http_or_template",
        health_http=http,
        health_error_code=error,
        message=(hints.get(error) or "健康接口不是预期模板或返回 HTTP 错误，检查域名路由和部署内容")
        + "；未切换主线路",
    )
