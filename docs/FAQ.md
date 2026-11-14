# imOK 常见问题 / FAQ

## 这是给谁用的？

适合希望在兼容 OpenWrt 路由器上统一管理 Cloudflare WARP、按设备或 LAN 分流，以及排查 DNS / 出口问题的用户。首次部署可由插件在线准备兼容运行环境，需要路由器能访问下载源，并准备自己的 Cloudflare Zero Trust 团队。

## 下载 IPK 就能在新路由器上使用吗？

新设备还要安装 WARP 和 Debian 组件。请用完整安装包里的首装程序补齐依赖，再到「WARP 安装与更新」查看在线安装进度。已有兼容运行环境可备份后直接升级 IPK。实机升级在 GL-MT6000 / ARM64 / OpenWrt 24.10 验证。[安装说明](../packages/luci-app-routepilot/INSTALL.zh-CN.md)

## 每台手机、电脑都要安装代理客户端吗？

对被路由器接管、且支持的 IPv4 流量，策略在路由器执行，终端可共用网络规则。终端自行开启的其他隧道、加密 DNS、IPv6 或不受管网络仍需分别理解；本项目不代表所有设备和应用都已验收。

## 必须有 VPS 吗？能指定国家吗？

WARP 默认线路不要求自建 VPS。要使用自己的 VPS 出口，需要先在 Cloudflare 组织端部署对应路由，并让账户能读到虚拟网络。插件选择已有网络，不会自动购买、创建 VPS，也不是任意国家选择器。出口名称只是一条标签，实际地区应检测确认。

## 固定 / 优选 Cloudflare 入口有什么用？

用于选择连接 Cloudflare 的入口路径，可能因本地网络不同而表现不同，不保证提速。它不是网站看到的出口 IP，也不能靠换入口直接选择国家。三种方式为官方自动、固定候选、手写 IPv4；候选同步读取官方客户端已下载的配置。可手动测量列表中的 IPv4 候选；测量结果不会自动应用。点击“应用并检测”才会尝试重连并验证原预期出口。

## WARP 会自动更新吗？

在「WARP 安装与更新」可以管理检查频率。默认每天检查官方目录，也可选择三天、每周或关闭。发现新版后会提示，由用户确认是否升级。

已有环境的插件更新、版本读取和调度已实测。全新 WARP 安装、APT 升级与运行环境恢复仍待独立实机验收。[WARP 维护说明](WARP_RUNTIME.md)

## 会自动切到 Workers 备用吗？

不会。Workers 的部署、保存、检测和启用是不同状态，需要自己的授权 / 节点、PROXYIP 和独立检测，最后手动启用；当前仅支持 IPv4 TCP。部署成功不能当作网络可用证明。

## 会覆盖原来的 SSR 或路由器配置吗？

插件不改写原 SSR、系统 network/firewall/dhcp 配置文件，但开启后会维护自身运行时路由、nft、命名空间和 DNS 接管。关闭会撤回自己的接管规则，操作前请先备份。默认情况下，主线路故障撤回接管时可能恢复宽带直连。

## WARP 启动时用哪个 DNS？

新建 WARP 实例时，imOK 在独立网络环境里短测当前宽带下发的 IPv4 DNS。能正常解析 Cloudflare 公共域名的宽带 DNS 优先使用，`223.5.5.5` 排在备用位置。找不到有效宽带 DNS 时，会短测候选表。候选均失败时沿用最近成功地址与公共兜底。指定和自定义模式可在 DNS 页配置，下次启动生效。这个设置只涉及 WARP 启动解析，设备 DNS、WARP 入口和账户出口仍由各自设置决定。

## 如何防止路由器重启时设备先走宽带直连？

接管范围选「按设备选择」，然后到 **Cloudflare 配置中心 → 设备 → 编辑设备策略**，为使用代理策略的设备打开「断网保护」，再保存并应用。新设备默认关闭。旧版靠全局设置或「跟随总设置」保护的设备，需要逐台明确启用。原来已明确启用的设备保留设置。

启用前请在 OpenWrt 防火墙关闭软、硬件流量卸载。检测到冲突时，imOK 会在保存前提示，原联网设置保持不变。

imOK 启动时，受保护设备先暂停外网转发和普通 DNS，等出口、转发和 DNS 确认就绪后恢复分流。检测到线路中断时重新阻断。其他设备继续按原有策略联网。放行后，匹配直连规则的流量仍可使用宽带。受保护设备的转发 IPv6 会被阻断，同一局域网内的访问和路由器管理仍可用。手动在总览关闭 imOK 后，分流、DNS 接管和断网保护全部撤回，设备恢复路由器原有的联网方式，可能使用宽带真实 IP。已保存的策略会保留，下次开启时重新生效。

在 **总览 → 详情与诊断 → 断网保护记录** 可以查看本次启动最近 40 条状态变化，包括阻断、恢复、关闭和保护未确认的原因。记录保存在运行内存中，路由器重启后重新记录。

如果希望 Mac mini 的应用始终使用代理出口，选「全部代理」，检查自定义直连规则，并给实际使用的网卡固定 DHCP 地址。Mac mini 自行连到其他网络时，由那个网络负责转发。

## 速度和长期稳定性如何？

速度与稳定性取决于宽带、Cloudflare 入口和自有出口的实际网络状况。当前正式版在 GL-MT6000 上验证了保留设置升级与联网。复现异常时请记录时间、网络环境和脱敏错误，通过 Issues 反馈。

## 支持 WireGuard 或 IPv6 代理吗？

独立客户端可选择 MASQUE 或 WireGuard，也可以单独选择账户已有出口。路由器代理 IPv4，IPv6 可选择阻断或允许直连。允许直连时，IPv6 可走本地宽带。[使用说明](../packages/luci-app-routepilot/README.zh-CN.md)

## Is this a complete first-install image?

The imOK install kit includes the OpenWrt plugin and a first-install script. The router downloads the compatible Debian and WARP components online. Use an existing Cloudflare team to register your router and select an egress.

## Does changing an entry IP change my exit country?

No. Entry selection controls how the router reaches Cloudflare. The active WARP virtual network / VPS route or backup determines the exit. Country labels must be confirmed by actual exit observation.

## Does Workers backup fail over automatically?

No. The backup supports IPv4 TCP and requires independent testing and manual enablement. The current regular release is v0.2.36-1 (imOK 2.0); other hardware and network environments require their own validation.

## 排查时会看到哪些内部名称？

| 界面或用途 | 内部名称 |
| --- | --- |
| imOK 插件 | `luci-app-routepilot` |
| UCI 配置 | `routepilot` |
| 规则和本地数据 | `/etc/routepilot/` |
| 运行状态 | `/var/run/routepilot/` |
| 策略路由 | `51990` |
| nft 分流与保护 | `inet routepilot`、`inet routepilot_egress_guard` |

受管设备可能命中 imOK 提前插入的转发放行规则。排查设备的 fw4 自定义转发限制时，同时检查 imOK 的转发链和路由选择。完整范围见[已知限制](KNOWN_LIMITS.md)。
