# 客户端接入（真实注册与导出已验证）

客户端接入是 imOK 的第五个主板块，路径为 `/admin/services/routepilot/clients`。它管理手机等设备的独立 MASQUE 或 WireGuard 注册与小火箭导入配置。LAN 分流仍在“设备”页面管理。

v0.2.37-4 新增 WireGuard 直接接入，并统一沿用本机配置的 Cloudflare 团队。独立 MASQUE 客户端已实测美国出口；独立 WireGuard 客户端在小火箭实测首尔出口与正常联网。MASQUE 固定订阅的 HTTPS 内容同步与停止也已验证。手机实际订阅导入、吞吐量和其他组织策略仍需分别确认，资料核验不代表手机已连接。

## 使用流程

1. 在「账户与协议」配置本机的 Cloudflare 团队。新增客户端时填写设备名称并选协议，默认 MASQUE，团队自动继承。现有注册保留原有协议；同一台手机若要使用另一协议，可新增一个独立客户端。
2. 进入“授权并注册”，选择邮箱授权或服务授权。邮箱方式需打开官方团队授权页，登录后将官方授权链接或团队 JWT 粘贴回 imOK。若出现“打开 WARP／Cloudflare One”提示，先取消，再复制链接。已配置服务授权的团队可在 imOK 直接填写或复用服务令牌。
3. 确认条款后提交。MASQUE 使用独立 P-256 密钥，WireGuard 使用独立 X25519 密钥。
4. 后台申请新注册，核对团队、协议、公钥和虚拟地址。
5. 点击“同步凭据与出口”，读取此独立身份的云端出口目录。明确选择美国／首尔等非默认网络，应用并读回确认后，才能导出。
6. 核对分配的 IPv4／IPv6、入口端口和 DNS 后导入。MASQUE 支持直接导入、二维码和固定私密订阅。WireGuard 支持 `.conf`、二维码和复制配置全文导入。手机实际连接结果仍须在手机上核验。
7. 不再使用时可先停止私密订阅，再输入名称确认撤销。停止订阅仅移除云端配置，不会撤销已导入的节点；删除本地记录前须确认注册已撤销且订阅已停止。

### 免邮箱登录的服务授权

1. 团队管理员在 Cloudflare Zero Trust 创建一个专用服务令牌，保存该令牌的 Client ID 和 Client Secret。[Cloudflare 服务令牌](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
2. 在团队的设备注册权限中添加一条 `Service Auth` 规则，匹配该服务令牌，保留现有邮箱规则。使用服务身份时，依赖登录邮箱或身份提供商的策略需单独评估。[Cloudflare 无交互设备注册](https://developers.cloudflare.com/cloudflare-one/tutorials/deploy-client-headless-linux/)
3. 在 imOK 的“授权并注册”中选择“填写服务令牌”，确认后提交。勾选保存选项后，成功注册时会将凭据保存在路由器的客户端私有目录，供同一团队后续新增设备时使用。
4. 令牌轮换后，可在下一次注册时填写新值并勾选替换。需要移除本地凭据时，用“清除本机服务授权”。Cloudflare 上的令牌和已注册设备可各自独立管理。

### 参数

| 参数 | 行为 |
| --- | --- |
| 团队名称 | 从「账户与协议」读取。已有客户端保留注册时的归属；其他团队的历史草稿不能用来申请新注册 |
| 虚拟 IPv4／IPv6 | 从新注册结果读取，只读；不支持自己改尾号 |
| 入口 IPv4 | MASQUE 使用 `162.159.197.0/24`，WireGuard 使用 `162.159.193.0/24`。WireGuard 首次核验后采用云端返回的入口 |
| UDP 端口 | MASQUE 默认 443，可选 500、1701、4500、4443、8443、8095。WireGuard 默认 2408，可选 500、1701、4500 |
| DNS | 每个客户端可修改；私有域名仍依赖相应内部解析配置 |
| MTU | 默认 1280，可在高级参数中设置 1280–1420 |
| 心跳间隔 | MASQUE 默认 30 秒，WireGuard 默认 25 秒；可选范围 10–120 秒 |
| 其他导出项 | MASQUE 包含 SNI、cubic 与 UDP 转发设置。WireGuard 配置包含 Cloudflare 分配的 Reserved 值 |

保存参数不切换路由器入口，也不主动修改手机当前连接。已启用私密订阅时，“保存并更新订阅”会同步新内容，手机仍需更新订阅后重连；直接导入则需重新导入。不同客户端可选择不同端口。端口和节点名称不决定出口国家。

### 导入与订阅

| 项目 | 直接导入／二维码 | 固定私密订阅 |
| --- | --- | --- |
| 首次接入 | 导入 MASQUE 节点，或导入 WireGuard 配置 | MASQUE 可将固定 HTTPS 地址作为订阅添加 |
| 端口、入口 IP、DNS 改动 | 重新导入配置 | imOK 保存并更新内容，手机更新订阅 |
| 美国／首尔出口调整 | imOK 应用云端选择，手机重连 | 同样只需重连，不换订阅地址 |
| 使旧地址失效 | 撤销独立注册使节点失效 | 停止私密订阅使读取地址失效，节点撤销单独操作 |

订阅只存放对应手机的配置，保存在独立 `imok-clients-*` Worker 的 Secret 中；不复用代理备用 Worker，不开放路由器管理端口，不创建新的网络隧道。首次发布优先使用已授权自有区域的独立子域名；已有地址不自动迁移。使用 Workers 页面当前保存的受限 Cloudflare API 授权，每次发布重新读取，账户改变时拒绝改写旧服务。订阅地址相当于读取此手机私钥的凭据，不应分享。列表不返回私钥、订阅 Token 或完整地址；仅在管理员确认显示后交付，关闭／Esc／两分钟后清除。

## 已实现与尚未验证

| 能力 | 开发状态与证据边界 |
| --- | --- |
| 本地独立客户端记录 | 已实现，最多 32 条；本地生命周期测试通过 |
| 网页 JWT 注册适配器 | 已在当前租户完成一次真实注册；同时保留隔离异常与生命周期测试 |
| 独立密钥生成 | 本机及目标路由器实际 OpenSSL 生成、DER 解析与独立性检查通过 |
| 注册状态刷新 | 只查询本模块保存的注册；不连接代理隧道 |
| 二维码、链接与文件导出 | 已实现；真实生成链接、原生下载文本与离线二维码解码一致 |
| 撤销与删除 | 已实现；只用本记录的设备凭据和注册 ID；云端请求仍未做真机验证 |
| 美国／首尔 VNET 绑定 | 已实现每注册云端选择与读回；美国绑定后用户确认 Codex 恢复。不在 URI 中伪造 VNET 参数 |
| 固定私密订阅 | 已实现独立 Worker Secret、固定地址、内容同步、HTTPS 读回与停止；已完成受测租户 HTTPS 内容与停止验证 |
| 服务令牌自动入网 | 已实现团队服务授权、同团队可选复用及清除。本机直连授权交换和隔离设备的小火箭导入已实测；imOK 后台流程另用隔离回归验证 |
| 手机隧道、出口与吞吐量 | 用户确认美国绑定后手机网络及 Codex 可用；手机公网出口 IP 与吞吐量未自动检测 |
| WireGuard 直接接入 | 用独立测试注册核对了 WireGuard、首尔出口和导出配置。用户在小火箭验证可联网，实际出口为首尔。插件内流程另做隔离回归 |

出口选择通过该独立注册的云端接口完成，不依赖第三方 URI 的地区字段。云端已选择、隧道已连接与实际出口已检测分别展示。[Cloudflare 虚拟网络说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/private-net/cloudflared/tunnel-virtual-networks/)

## 授权与协议边界

- 注册适配器参考 [usque 的公开源码](https://github.com/Diniboy1123/usque/blob/main/api/cloudflare.go)，固定访问 `https://api.cloudflareclient.com/v0a4471`。这是客户端注册协议，不是 Cloudflare 承诺稳定的公共管理 API。
- 只允许固定的注册路径，以及经过自身团队、注册与公钥核对的每注册虚拟网络路径，验证 HTTPS 证书，不跟随重定向、不读取代理环境变量。接口返回文本不直接回显。
- JWT 的本地解析仅检查格式、团队和过期时间，签名与入网权限由 Cloudflare 验证。不会自行绕过组织策略、设备姿态或硬件密钥要求。
- 首次返回的身份 ID 和设备 Token 先安全记录，再核对是否确属目标团队；不会静默退回个人 WARP 或为它生成导入链接。
- 入网授权与 Workers API 授权分开。服务令牌只发给目标团队的 HTTPS 授权页，客户端注册使用该页返回的短期授权。服务身份的策略与邮箱登录身份不同。[官方入网权限](https://developers.cloudflare.com/cloudflare-one/team-and-resources/devices/cloudflare-one-client/deployment/device-enrollment/)
- Workers 首次绑定或更换 Cloudflare 账户时，需让受限 API 令牌读取目标账户的 Zero Trust 组织。imOK 核对团队域名后才保存账户。已保存的账户和部署继续使用原授权。[组织读取权限](https://developers.cloudflare.com/api/resources/zero_trust/subresources/organizations/methods/list/)
- IP 绑定注册，不是任意生成的文本；获取一个已有 IP 也不能取回相应私钥。[设备 IP](https://developers.cloudflare.com/cloudflare-one/team-and-resources/devices/cloudflare-one-client/configure/device-ips/)、[注册查询 API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/devices/subresources/registrations/methods/get/)

## 与主线路隔离

后端不导入 `mplane`、`masque`、`auth_control`、`warp_control` 或 `endpoint_control`。不执行 `warp-cli`、UCI、服务启停、网络命名空间、路由、DNS 或分流命令。不存在“暂时停用主线路进行注册”的步骤。

- 新身份保存在 `/etc/routepilot/clients/registry.json`，另有 `initialized.json` 保护标记；目录 0700，文件 0600，不放进 UCI。
- 只读程序 `routepilot-clients-list` 与写操作程序 `routepilot-clients` 分开授权。只读 ACL 不允许执行后者或读取私钥目录。
- 邮箱 JWT 和一次性服务令牌只进入 0600 的易失任务文件，任务领取后删除。勾选保存并成功注册时，服务令牌独立保存在团队专属 0600 文件中，列表只返回是否已保存。过期未领取任务会清理。
- 后台任务单独运行，没有常驻的第二个 WARP 服务；仅在客户端任务进行时每两秒读取本地状态。空闲页面不持续查询 Cloudflare。
- 私钥只在管理员明确导出或创建私密订阅时交付；列表不含密钥、设备 Token、登录令牌或订阅 Token。云端订阅仅包含该手机配置，不包含路由器私钥或完整备份。
- 二维码在浏览器本地生成，不向二维码网站发送链接。离线编码器为 qrcode-generator 2.0.4，MIT 许可证与来源校验记录随包提供。
- 导出显示在关闭、Esc、切换标签页或两分钟后清除。此显示期限不会让已经复制的链接失效；它仍须通过云端撤销来失效。

现有 imOK v2/v3 普通设置导出不包含新身份，格式和恢复行为不变。OpenWrt 整机备份／固件迁移则通过 keep.d 保留客户端私有目录，因此整机备份应视为包含凭据的敏感文件。

## 中断与失败处理

注册前记录独立密钥和预注册指纹；重复点击不会启动第二个任务。注册成功返回 ID 后立即记录归属，后续密钥登记失败可以用刷新查询核对，不重复申请新设备。团队返回的 `t.<UUID>` 注册 ID 保留前缀；精简 OpenWrt 环境显式依赖 `python3-codecs`，请求前会检查 IDNA。

对于已收到但暂时无法解析的返回，先在私有目录保存脱敏的登记回执以便接续，不对只读页面开放。若首次 POST 的响应丢失、尚未拿到设备 ID／Token，页面显示“云端结果待核对”，保留预注册公钥指纹和请求序列，禁止自动重试和删除归属记录。需要在 Cloudflare 侧核对新增注册后处理；本版不声称能自动找回丢失的设备 Token，也不提供强行清除待核对归属记录的按钮。

撤销使用该记录的设备凭据删除单个注册，不调用全组织撤销；失败或结果未知时保留本地凭据和归属记录，不先显示成功。记录损坏或权限错误时停止操作，不自动清空重建。

本次进一步检查了原生弹窗兼容性、撤销确认、身份冲突、任务超时、注册表损坏与异步交互，并修复实际问题。详见[本版质量报告](quality/system-audit-20261109.md)。

## 本地预览与复核

```sh
PYTHONDONTWRITEBYTECODE=1 python3 packages/luci-app-routepilot/tools/ui_preview/server.py --port 8772
```

打开 `http://127.0.0.1:8772/cgi-bin/luci/admin/services/routepilot/clients`。所有预览记录、密钥、注册和撤销结果都是模拟数据；预览不代理生产接口。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s packages/luci-app-routepilot/tests -p test_client_access.py -v
node packages/luci-app-routepilot/tests/test_client_access.cjs
ego-browser nodejs < packages/luci-app-routepilot/tools/ui_preview/client-access-audit.mjs
```

全量检查仍使用项目 `tools/check_all.py`，需指定已授权缓存的目标 LuCI core。它仅在临时目录做可复现构建，不产生正式发布包，也不安装路由器。

## 升级与恢复

普通升级先备份旧 IPK、配置和私人身份，再安装新版完整插件 IPK，不重注册、不覆盖运行环境。源码或插件回退不会自动撤销云端注册；需要使手机节点失效时，应先通过对应身份撤销，不要直接删除本地归属记录。

普通设置导出不是完整身份备份。跨设备迁移、旧版恢复与首次安装的步骤见[安装说明](../packages/luci-app-routepilot/INSTALL.zh-CN.md)。
