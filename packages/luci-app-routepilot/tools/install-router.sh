#!/bin/sh
# First install or explicit plugin upgrade; run only on the target router.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE=${1:---install}
case "$MODE" in --install|--upgrade|--check) ;; *) echo '用法：sh install-router.sh [--install|--upgrade|--check]'; exit 1;; esac
[ -r /etc/openwrt_release ] || { echo '请在 ARM64 OpenWrt 路由器运行，不要在 Mac 或 Windows 运行'; exit 1; }
. /etc/openwrt_release
case "$DISTRIB_RELEASE:$(uname -m)" in 24.10*:aarch64) ;; *) echo '此包仅验证 ARM64 OpenWrt 24.10；其他架构 / 固件不安装'; exit 1;; esac
set -- "$HERE"/luci-app-routepilot_*.ipk
[ "$#" -eq 1 ] && [ -f "$1" ] || { echo '安装目录必须只包含本版的一个 imok IPK'; exit 1; }
PKG=$1
RUNTIME="$HERE/routepilot-runtime-arm64_2026.8.2115.0.tgz"
[ -f "$HERE/SHA256SUMS" ] || { echo '缺少 SHA256SUMS'; exit 1; }
if [ "$MODE" = --install ] || { [ "$MODE" = --check ] && [ ! -d /opt/routepilot/runtime ]; }; then
 [ -f "$RUNTIME" ] || { echo '缺少运行环境包'; exit 1; }
 grep -Fq "  $(basename "$RUNTIME")" "$HERE/SHA256SUMS" || { echo 'SHA256SUMS 未包含运行环境包校验项，停止安装'; exit 1; }
fi
grep -Fq "  $(basename "$PKG")" "$HERE/SHA256SUMS" || { echo 'SHA256SUMS 未包含当前 IPK 校验项，停止安装'; exit 1; }
(cd "$HERE" && sha256sum -c SHA256SUMS) || { echo '文件校验失败，停止安装'; exit 1; }
[ ! -L /opt/routepilot ] && [ ! -L /opt/routepilot/runtime ] || { echo '运行环境路径为符号链接，请先人工核对'; exit 1; }
DF_TARGET=/overlay
[ -d "$DF_TARGET" ] || DF_TARGET=/
FREE=$(df -Pk "$DF_TARGET" | awk 'NR==2 {print $4}')
if [ ! -e /opt/routepilot/runtime ]; then
 [ "${FREE:-0}" -ge 512000 ] || { echo '首次安装至少需要 500 MB 可用存储'; exit 1; }
fi
if [ "$MODE" = --check ]; then
 echo '校验通过；未安装、未保存配置、未改变运行线路。'
 if [ -d /opt/routepilot/runtime ]; then echo '已有运行环境：升级请用 --upgrade，保留原注册身份。'; else echo '没有运行环境：首次安装可使用 --install。'; fi
 if ! command -v xray >/dev/null 2>&1; then echo 'Workers 备用需要 xray-core，正式安装时从当前固件匹配的软件源安装。'; fi
 exit 0
fi
[ "$(id -u)" = 0 ] || { echo '安装请以路由器管理员运行'; exit 1; }
if [ "$MODE" = --install ] && [ -e /opt/routepilot/runtime ]; then echo '已有运行环境，拒绝覆盖；请改用 --upgrade'; exit 1; fi
if [ "$MODE" = --upgrade ] && [ ! -x /opt/routepilot/runtime/usr/bin/warp-svc ]; then echo '未找到完整旧运行环境，请核对后使用首次安装'; exit 1; fi
command -v opkg >/dev/null 2>&1 || { echo '未找到 opkg；此固件不在当前支持范围'; exit 1; }
echo '更新软件包索引并安装缺少的依赖；不升级固件，不使用强制依赖。需要正常联网。'
opkg update
if ! command -v chroot >/dev/null 2>&1; then opkg install coreutils-chroot; fi
if ! command -v xray >/dev/null 2>&1; then opkg install xray-core; fi
if [ "$MODE" = --install ]; then
 mkdir -p /opt/routepilot
 chmod 700 /opt/routepilot
 STAGE="/opt/routepilot/.runtime-new-$$"
 mkdir "$STAGE"
 trap '[ ! -d "$STAGE" ] || rm -rf "$STAGE"' EXIT HUP INT TERM
 tar --no-same-owner --no-same-permissions -xzf "$RUNTIME" -C "$STAGE"
 [ -x "$STAGE/usr/bin/warp-cli" ] && [ -x "$STAGE/usr/bin/warp-svc" ] || { echo '运行环境不完整，停止安装'; exit 1; }
 chown 0:0 "$STAGE/usr/bin/warp-cli" "$STAGE/usr/bin/warp-svc"
 mv "$STAGE" /opt/routepilot/runtime
fi
if ! opkg install "$PKG"; then echo '插件安装未完成。运行环境保留；请先核查依赖 / 软件源，不要删除原注册信息。'; exit 1; fi
echo '安装完成：LuCI → 服务 → imok → Cloudflare 配置中心。'
echo '首次安装默认关闭，需在自己的账户登录后选择出口与接管范围，再手动启用。'
echo '升级保留已保存配置，可能短暂重连并按原启用状态恢复。Workers 仍需独立检测、手动启用。'
