#!/usr/bin/env bash
# 币安 U 本位合约监控 - 服务器一键安装脚本
# 适用：Ubuntu / Debian（阿里云、腾讯云、Vultr 等轻量服务器）
# 用法：sudo bash install.sh
set -euo pipefail

APP_DIR="/opt/binance-monitor"
SVC="binance-monitor"
RAW="https://raw.githubusercontent.com/renq7147-ctrl/binance-futures-monitor/main"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf "\033[36m[i]\033[0m %s\n" "$*"; }
ok()  { printf "\033[32m[✓]\033[0m %s\n" "$*"; }
die() { printf "\033[31m[✗]\033[0m %s\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"

if ! command -v python3 >/dev/null 2>&1; then
  say "安装 python3 ..."
  apt-get update -qq && apt-get install -y -qq python3
fi
if ! command -v curl >/dev/null 2>&1; then
  say "安装 curl ..."
  apt-get update -qq && apt-get install -y -qq curl
fi

mkdir -p "$APP_DIR"
say "安装目录：$APP_DIR"

if [ -f "$HERE/binance_monitor.py" ]; then
  install -m 644 "$HERE/binance_monitor.py" "$APP_DIR/binance_monitor.py"
else
  say "下载主程序 ..."
  curl -fsSL "$RAW/binance_monitor.py" -o "$APP_DIR/binance_monitor.py"
fi

BASE_CFG=""
for c in "$HERE/config.json" "$HERE/config.cloud.json"; do
  if [ -f "$c" ]; then BASE_CFG="$c"; break; fi
done
if [ -z "$BASE_CFG" ]; then
  curl -fsSL "$RAW/config.cloud.json" -o /tmp/base_cfg.json
  BASE_CFG=/tmp/base_cfg.json
fi

WEBHOOK="${WECOM_WEBHOOK:-}"
if [ -z "$WEBHOOK" ]; then
  printf "请粘贴企业微信群机器人 Webhook 地址，回车确认：\n> "
  read -r WEBHOOK
fi
case "$WEBHOOK" in
  https://qyapi.weixin.qq.com/*) : ;;
  "") die "Webhook 不能为空" ;;
  *) die "这个地址不像企业微信机器人 Webhook（应以 https://qyapi.weixin.qq.com/ 开头）" ;;
esac

python3 - "$BASE_CFG" "$APP_DIR/config.json" "$WEBHOOK" <<'PYEOF'
import json
import sys

src, dst, hook = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(src, encoding="utf-8"))
cfg["interval_seconds"] = 60
cfg["proxy"] = ""
cfg["auto_detect_proxy"] = False
cfg["push"]["wecom_bot"] = {"enabled": True, "webhook_url": hook}
cfg["push"]["serverchan"]["enabled"] = False
if "pushplus" in cfg["push"]:
    cfg["push"]["pushplus"]["enabled"] = False
json.dump(cfg, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("[i] 配置已生成：阈值 %s%% / 间隔 %ss / 冷却 %ss"
      % (cfg["threshold_percent"], cfg["interval_seconds"], cfg["cooldown_seconds"]))
PYEOF

say "连通性自检 ..."
cd "$APP_DIR"
if ! python3 binance_monitor.py --check --config "$APP_DIR/config.json"; then
  cat <<'TIP'

[✗] 自检失败。最常见的原因是这台服务器所在地区被币安封锁：
    出现 HTTP 451 / 连接失败 => 请换香港、新加坡、日本节点重装。
    另外请确认这台机器能正常访问外网（curl -I https://www.google.com）。
TIP
  exit 2
fi

say "写入系统服务 ..."
cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=Binance USDT-M futures pump monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/python3 ${APP_DIR}/binance_monitor.py --config ${APP_DIR}/config.json
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1
systemctl restart "$SVC"
sleep 6

say "服务状态："
systemctl is-active "$SVC" | sed 's/^/    /'
echo
ok "安装完成。常用命令："
cat <<'CMD'

    systemctl status binance-monitor      查看状态
    journalctl -u binance-monitor -f      实时看日志
    systemctl restart binance-monitor     重启
    systemctl stop binance-monitor        停止

    告警日志同时在 /opt/binance-monitor/logs/ 下按天保存。

CMD
