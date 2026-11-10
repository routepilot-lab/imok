# imOK 常见问题 / FAQ

## 这是给谁用的？

适合希望在兼容 OpenWrt 路由器上统一管理 Cloudflare WARP、按设备或 LAN 分流，以及排查 DNS / 出口问题的用户。当前首次部署仍需自行准备兼容运行环境，适合愿意维护自己线路的用户。

## 下载 IPK 就能在新路由器上使用吗？

不能。公开 IPK 是插件包，不含 Cloudflare 客户端和 Debian 运行环境。已有兼容运行环境可先备份再升级插件；新设备先阅读[安装说明](../packages/luci-app-routepilot/INSTALL.zh-CN.md)，另行合法准备未注册环境及固件依赖。当前现场验证机为 GL-MT6000 / ARM64 OpenWrt 24.10，不能直接推断其他设备兼容。

## 每台手机、电脑都要安装代理客户端吗？

对被路由器接管、且支持的 IPv4 流量，策略在路由器执行，终端可共用网络规则。终端自行开启的其他隧道、加密 DNS、IPv6 或不受管网络仍需分别理解；本项目不代表所有设备和应用都已验收。

## 必须有 VPS 吗？能指定国家吗？

WARP 默认线路不要求自建 VPS。要使用自己的 VPS 出口，需要先在 Cloudflare 组织端部署对应路由，并让账户能读到虚拟网络。插件选择已有网络，不会自动购买、创建 VPS，也不是任意国家选择器。出口名称只是一条标签，实际地区应检测确认。

## 固定 / 优选 Cloudflare 入口有什么用？

用于选择连接 Cloudflare 的入口路径，可能因本地网络不同而表现不同，不保证提速。它不是网站看到的出口 IP，也不能靠换入口直接选择国家。三种方式为官方自动、固定候选、手写 IPv4；候选同步仅读取官方客户端已下载的本地配置，不主动强制云端更新、不测速、不自动替换固定入口。点击“应用并检测”才会尝试重连并验证原预期出口。

## WARP 会自动更新吗？

v0.2.37-1 预发布新增“WARP 安装与更新”。默认每天检查官方目录，也可选择三天、每周或关闭；发现新版提示，**不会自动安装、升级或重启主线路**。下载准备与确认安装分开，运行环境维护需明确确认。

已有环境的插件更新、版本读取和调度已实测；全新 WARP 安装、APT 升级与运行环境恢复仍待独立实机验收。旧正式版 v0.2.36-1 继续保留。完整流程见 [WARP 维护说明](WARP_RUNTIME.md)。

## 会自动切到 Workers 备用吗？

不会。Workers 的部署、保存、检测和启用是不同状态，需要自己的授权 / 节点、PROXYIP 和独立检测，最后手动启用；当前仅支持 IPv4 TCP。部署成功不能当作网络可用证明。

## 会覆盖原来的 SSR 或路由器配置吗？

插件不改写原 SSR、系统 network/firewall/dhcp 配置文件，但开启后会维护自身运行时路由、nft、命名空间和 DNS 接管。关闭会撤回自己的接管规则；仍应先备份，避免将“配置文件不变”理解成“运行态没有变化”。主线路故障撤回接管时可能宽带直连；恢复无法确认时另有阻断保护。

## 速度和长期稳定性如何？

当前保留 v0.2.36-1 正式版，并提供 v0.2.37-1 WARP 维护预发布版。预发布的现有环境更新与版本检查已实测，首次安装和运行环境写入仍有明确验证边界。没有保证手机速度、固定带宽或长期不掉线。复现异常时请记录时间、网络环境和脱敏错误，通过 Issues 反馈。

## 支持 WireGuard 或 IPv6 代理吗？

界面保留官方客户端协议选择，但本机 WireGuard 切换尚未验收，不作为当前已验证能力宣传。本插件只代理 IPv4；IPv6 可选择阻断或允许直连，允许直连可能绕过代理规则。请阅读[使用说明](../packages/luci-app-routepilot/README.zh-CN.md)。

## Is this a complete first-install image?

No. imok is a LuCI plugin, not router firmware or a bundled Cloudflare runtime. The published IPK updates an existing compatible runtime. Prepare runtime software and firmware-specific dependencies separately, and authorize your own Cloudflare account.

## Does changing an entry IP change my exit country?

No. Entry selection controls how the router reaches Cloudflare. The active WARP virtual network / VPS route or backup determines the exit. Country labels must be confirmed by actual exit observation.

## Does Workers backup fail over automatically?

No. The backup supports IPv4 TCP and requires independent testing and manual enablement. The current regular release is v0.2.36-1 (imOK 2.0); other hardware and network environments require their own validation.
