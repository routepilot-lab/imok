# 总览隔离预览

运行真实候选 view 和 feedback 模块，使用合成数据、原生DOM及轻量 LuCI 表单适配器。所有 fs/uci/RPC 在内存中模拟，不代理路由器接口；HTTP服务仅监听127.0.0.1，拒绝POST。仅总览操作提供交互模拟，其余十一页用于结构和共享排版回归，不能代替真实LuCI表单验收。

从项目根目录运行：

```sh
python3 -B packages/luci-app-routepilot/tools/ui_preview/server.py
```

访问 http://127.0.0.1:8767/cgi-bin/luci/admin/services/routepilot/overview 。场景选择器可查看连接、网站检测失败、关闭、等待应用、过期、空数据、异常连接、权限失败、读取中断、异常JSON和手动备用。开关只修改预览内存。

主题CSS需要预先放在 `.local/fixtures/argon-cascade.css`。本次已通过只读GET缓存实际Argon 2.2.9.4样式。缓存不入Git、不打包进插件，没有另行安装或分发字体。复现时应使用同一授权主题文件；服务不会自动访问生产设备取配置。

预览 `?baseline=1` 从 `pre-imok-ui-v2-20261108` Git标记读取原始view/CSS，使用同一适配器对照。切勿把该模拟预览当作线上安装版本。

启动服务后，可使用ego-browser执行：

```sh
ego-browser nodejs < packages/luci-app-routepilot/tools/ui_preview/browser-audit.mjs
ego-browser nodejs < packages/luci-app-routepilot/tools/ui_preview/interaction-audit.mjs
```

记录与截图写入忽略目录 `.local/ui-v2`。脚本使用同一专用TaskSpace，完成全部检查后由操作者结束测试空间。浏览器脚本中的等效视口/双倍根文字检查不等同于真实浏览器200%缩放；本次原生缩放尚未验证。

原有全量检查只在临时目录构建并自动清理测试IPK，不生成发行包：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 packages/luci-app-routepilot/tools/check_all.py \
  --luci-core .local/fixtures/luci.js \
  --report .local/ui-v2/checks-final/report.json
```

不要运行opkg安装、服务启停、生产开关、DNS清理、Cloudflare写操作或恢复流程来验证此预览。

`.local/fixtures/` 保存复测必需的既有 LuCI 核心与 Argon 样式输入，不能当历史测试缓存删除；新截图和检查结果仍写入各自的证据目录。
