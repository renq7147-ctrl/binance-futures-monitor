#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安 U 本位合约（USDT-M 永续）24 小时涨幅监控 + 微信推送

功能：
  1. 轮询 Binance USDT-M 永续合约 24h 行情（单次请求拉全市场）
  2. 当某合约 24h 涨幅 >= 阈值（默认 120%）时，立即通过微信推送
  3. 内置去重与冷却：同一标的不会重复轰炸，回落后可再次触发
  4. 支持代理（含本地常见端口自动探测），适配国内网络环境
  5. 推送通道：Server酱 / PushPlus / 企业微信群机器人 / 自定义 Webhook

仅依赖 Python 标准库，无需 pip install。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
SYMBOL_CACHE = BASE_DIR / ".symbols_cache.json"
LOG_DIR = BASE_DIR / "logs"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BinanceFuturesMonitor/1.0"
SYMBOL_CACHE_TTL = 12 * 3600

# 常见的本地代理端口（Clash / v2rayN / Shadowsocks / Surge 等）
COMMON_PROXY_PORTS = [7890, 7897, 7891, 7899, 10809, 10808, 1080, 2080, 33210, 6152, 8118]

DEFAULT_BASES = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
]

log = logging.getLogger("binance_monitor")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def setup_logger(verbose: bool = True) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"monitor-{datetime.now():%Y%m%d}.log"

    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(fmt)
    log.addHandler(ch)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    s = f"{p:.8f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt_volume(v: float) -> str:
    if v >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f} 万"
    return f"{v:.0f}"


def http_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 12.0,
) -> tuple[int, bytes]:
    """发起 HTTP 请求，返回 (status_code, body)。网络异常直接抛出。"""
    if proxy:
        handlers: list[Any] = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    else:
        handlers = [urllib.request.ProxyHandler({})]  # 显式忽略系统代理，避免环境变量干扰
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_proxy(proxy: str, test_url: str = DEFAULT_BASES[0] + "/fapi/v1/ping", timeout: float = 6.0) -> bool:
    try:
        status, _ = http_request(test_url, proxy=proxy, timeout=timeout)
        return status == 200
    except Exception:
        return False


def resolve_proxy(cfg: dict, quiet: bool = False) -> str | None:
    """优先级：config.proxy > 环境变量 > 本地常见端口自动探测。"""
    configured = (cfg.get("proxy") or "").strip()
    if configured:
        if not quiet:
            log.info("使用配置的代理：%s", configured)
        return configured

    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        env_val = (os.environ.get(key) or "").strip()
        # 跳过明显是本机注入的服务代理（非翻墙代理）
        if env_val and "127.0.0.1" not in env_val and "localhost" not in env_val:
            if not quiet:
                log.info("使用环境变量代理（%s）：%s", key, env_val)
            return env_val

    if not cfg.get("auto_detect_proxy", True):
        return None

    if not quiet:
        log.info("未配置代理，尝试探测本地常见代理端口 ...")
    for port in COMMON_PROXY_PORTS:
        if not port_open("127.0.0.1", port):
            continue
        candidate = f"http://127.0.0.1:{port}"
        if probe_proxy(candidate):
            if not quiet:
                log.info("已自动探测到可用代理：%s", candidate)
            return candidate
        if not quiet:
            log.debug("端口 %s 开放但无法访问币安，跳过", port)

    if not quiet:
        log.warning("未发现可用代理，将尝试直连币安（国内网络通常不可达）")
    return None


# --------------------------------------------------------------------------- #
# 币安行情客户端
# --------------------------------------------------------------------------- #
class BinanceClient:
    def __init__(self, cfg: dict):
        self.bases = [b.rstrip("/") for b in (cfg.get("base_urls") or DEFAULT_BASES)]
        self.timeout = float(cfg.get("timeout_seconds", 12))
        self.proxy = resolve_proxy(cfg)
        self.base: str | None = None

    def _get_json(self, path: str, params: dict | None = None) -> Any:
        order: list[str] = []
        if self.base:
            order.append(self.base)
        order += [b for b in self.bases if b != self.base]

        errors: list[str] = []
        for base in order:
            url = base + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            try:
                status, body = http_request(url, proxy=self.proxy, timeout=self.timeout)
                if status == 200:
                    self.base = base
                    return json.loads(body.decode("utf-8"))
                errors.append(f"{base} -> HTTP {status}")
            except Exception as e:  # 网络错误、解析错误
                errors.append(f"{base} -> {type(e).__name__}: {e}")
            log.debug("请求失败：%s", errors[-1])

        raise RuntimeError("所有行情节点均不可用：" + " | ".join(errors[-3:]))

    def ping(self) -> bool:
        try:
            self._get_json("/fapi/v1/ping")
            return True
        except Exception:
            return False

    def load_symbols(self, force: bool = False) -> set[str]:
        """获取全部可交易的 USDT 本位永续合约（带 12 小时本地缓存）。"""
        if not force and SYMBOL_CACHE.exists():
            try:
                cached = json.loads(SYMBOL_CACHE.read_text(encoding="utf-8"))
                if time.time() - cached.get("ts", 0) < SYMBOL_CACHE_TTL and cached.get("symbols"):
                    return set(cached["symbols"])
            except Exception:
                pass

        info = self._get_json("/fapi/v1/exchangeInfo")
        symbols = {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("quoteAsset") == "USDT"
            and s.get("marginAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        }
        if symbols:
            try:
                SYMBOL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                SYMBOL_CACHE.write_text(
                    json.dumps({"ts": time.time(), "symbols": sorted(symbols)}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return symbols

    def fetch_tickers(self) -> list[dict]:
        data = self._get_json("/fapi/v1/ticker/24hr")
        if not isinstance(data, list):
            raise RuntimeError("行情返回格式异常")

        rows: list[dict] = []
        for t in data:
            try:
                rows.append(
                    {
                        "symbol": t["symbol"],
                        "last": float(t["lastPrice"]),
                        "pct": float(t["priceChangePercent"]),
                        "open": float(t.get("openPrice") or 0),
                        "high": float(t["highPrice"]),
                        "low": float(t["lowPrice"]),
                        "quote_volume": float(t["quoteVolume"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return rows


# --------------------------------------------------------------------------- #
# 推送通道
# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("push") or {}
        self.timeout = 15.0

    # -- 各通道 -------------------------------------------------------------
    def _serverchan(self, title: str, markdown: str) -> tuple[bool, str]:
        conf = self.cfg.get("serverchan") or {}
        key = (conf.get("send_key") or "").strip()
        if not key:
            return False, "Server酱未填写 send_key"
        url = f"https://sctapi.ftqq.com/{key}.send"
        if key.lower().startswith("sctp"):
            # Server酱³ 新版密钥：形如 sctp{uid}t{token}
            uid = key[4:].split("t")[0]
            if uid.isdigit():
                url = f"https://{uid}.push.ft07.com/send/{key}.send"

        payload = urllib.parse.urlencode({"title": title[:32], "desp": markdown}).encode("utf-8")
        status, body = http_request(
            url,
            method="POST",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        text = body.decode("utf-8", "replace")
        ok = status == 200 and '"code":0' in text.replace(" ", "")
        return ok, text[:300]

    def _pushplus(self, title: str, markdown: str) -> tuple[bool, str]:
        conf = self.cfg.get("pushplus") or {}
        token = (conf.get("token") or "").strip()
        if not token:
            return False, "PushPlus 未填写 token"
        payload = {
            "token": token,
            "title": title[:100],
            "content": markdown,
            "template": "markdown",
        }
        if conf.get("topic"):
            payload["topic"] = conf["topic"]
        status, body = http_request(
            "https://www.pushplus.plus/send",
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        text = body.decode("utf-8", "replace")
        ok = status == 200 and '"code":200' in text.replace(" ", "")
        return ok, text[:300]

    def _wecom_bot(self, title: str, markdown: str) -> tuple[bool, str]:
        conf = self.cfg.get("wecom_bot") or {}
        url = (conf.get("webhook_url") or "").strip()
        if not url:
            return False, "企业微信机器人未填写 webhook_url"
        content = f"**{title}**\n{markdown}"
        if len(content.encode("utf-8")) > 4000:
            content = content.encode("utf-8")[:3900].decode("utf-8", "ignore") + "\n...(已截断)"
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        status, body = http_request(
            url,
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        text = body.decode("utf-8", "replace")
        ok = status == 200 and '"errcode":0' in text.replace(" ", "")
        return ok, text[:300]

    def _custom_webhook(self, title: str, markdown: str) -> tuple[bool, str]:
        conf = self.cfg.get("custom_webhook") or {}
        url = (conf.get("url") or "").strip()
        if not url:
            return False, "自定义 Webhook 未填写 url"
        payload = {"title": title, "content": markdown, "text": f"{title}\n{markdown}"}
        status, body = http_request(
            url,
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        return 200 <= status < 300, body.decode("utf-8", "replace")[:300]

    # -- 对外接口 -----------------------------------------------------------
    def active_channels(self) -> list[str]:
        names = []
        for name in ("serverchan", "pushplus", "wecom_bot", "custom_webhook"):
            conf = self.cfg.get(name) or {}
            if conf.get("enabled"):
                names.append(name)
        return names

    def is_ready(self) -> bool:
        """是否至少有一个通道填好了凭证。"""
        checks = {
            "serverchan": bool((self.cfg.get("serverchan") or {}).get("send_key")),
            "pushplus": bool((self.cfg.get("pushplus") or {}).get("token")),
            "wecom_bot": bool((self.cfg.get("wecom_bot") or {}).get("webhook_url")),
            "custom_webhook": bool((self.cfg.get("custom_webhook") or {}).get("url")),
        }
        return any(checks.get(n) for n in self.active_channels())

    def send(self, title: str, markdown: str) -> bool:
        channels = [c for c in self.active_channels() if self._configured(c)]
        if not channels:
            log.error("没有可用的推送通道，请先在 config.json 中填写凭证。")
            return False

        handlers = {
            "serverchan": self._serverchan,
            "pushplus": self._pushplus,
            "wecom_bot": self._wecom_bot,
            "custom_webhook": self._custom_webhook,
        }
        any_ok = False
        for name in channels:
            try:
                ok, detail = handlers[name](title, markdown)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            if ok:
                any_ok = True
                log.info("推送成功 -> %s", name)
            else:
                log.error("推送失败 -> %s：%s", name, detail)
        return any_ok

    def _configured(self, name: str) -> bool:
        conf = self.cfg.get(name) or {}
        key_field = {
            "serverchan": "send_key",
            "pushplus": "token",
            "wecom_bot": "webhook_url",
            "custom_webhook": "url",
        }[name]
        return bool((conf.get(key_field) or "").strip())


# --------------------------------------------------------------------------- #
# 状态（去重 / 冷却）
# --------------------------------------------------------------------------- #
class StateStore:
    def __init__(self) -> None:
        self.data: dict = {"symbols": {}}
        self.loaded = False
        self._load()

    def _load(self) -> None:
        if STATE_PATH.exists():
            try:
                self.data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self.loaded = True
            except Exception:
                self.data = {"symbols": {}}

    def save(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(STATE_PATH)
        except Exception as e:
            log.warning("保存状态失败：%s", e)

    def entry(self, symbol: str) -> dict | None:
        return (self.data.get("symbols") or {}).get(symbol)

    def should_alert(self, symbol: str, pct: float, cooldown: int, reset_below: float) -> bool:
        entry = self.entry(symbol)
        now = time.time()
        if entry is None:
            return True
        if pct < reset_below:  # 已回落，清空记录，下次再突破可立即提醒
            self.clear(symbol)
            return True
        return now - float(entry.get("last_push", 0)) >= cooldown

    def mark(self, symbol: str, pct: float) -> None:
        self.data.setdefault("symbols", {})[symbol] = {"last_push": time.time(), "last_pct": round(pct, 4)}

    def clear(self, symbol: str) -> None:
        (self.data.get("symbols") or {}).pop(symbol, None)

    def prune(self, valid_symbols: set[str]) -> None:
        items = self.data.get("symbols") or {}
        for sym in list(items):
            if sym not in valid_symbols:
                items.pop(sym, None)


# --------------------------------------------------------------------------- #
# 消息构造
# --------------------------------------------------------------------------- #
def build_message(candidates: list[dict], threshold: float, total_scanned: int) -> tuple[str, str]:
    n = len(candidates)
    title = f"币安合约异动：{n} 个标的 24H 涨幅超 {threshold:g}%"

    lines = [
        f"**监控时间**：{now_str()}",
        f"**交易所**：Binance U 本位永续（USDT-M）",
        f"**触发条件**：24H 涨幅 ≥ {threshold:g}%",
        f"**本轮扫描**：{total_scanned} 个合约",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines += [
            f"**{i}. {c['symbol']}　+{c['pct']:.2f}%**",
            f"- 最新价：{fmt_price(c['last'])}",
            f"- 24H 开 → 现：{fmt_price(c['open'])} → {fmt_price(c['last'])}",
            f"- 24H 高 / 低：{fmt_price(c['high'])} / {fmt_price(c['low'])}",
            f"- 24H 成交额：{fmt_volume(c['quote_volume'])} USDT",
            "",
        ]
    lines.append("> 数据来源 Binance Futures API，仅供参考，不构成投资建议。")
    return title, "\n".join(lines)


def plain_preview(title: str, markdown: str) -> str:
    text = markdown.replace("**", "").replace("> ", "").replace("- ", "  ")
    return f"{title}\n{text}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def filter_candidates(
    tickers: list[dict],
    valid_symbols: set[str],
    cfg: dict,
    threshold: float,
) -> list[dict]:
    whitelist = {s.upper() for s in (cfg.get("symbols_whitelist") or [])}
    blacklist = {s.upper() for s in (cfg.get("symbols_blacklist") or [])}
    min_qv = float(cfg.get("min_quote_volume_usdt", 0) or 0)

    out = []
    for t in tickers:
        sym = t["symbol"]
        if sym not in valid_symbols:
            continue
        if whitelist and sym not in whitelist:
            continue
        if sym in blacklist:
            continue
        if t["quote_volume"] < min_qv:
            continue
        if t["pct"] >= threshold:
            out.append(t)
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out


def scan_once(client: BinanceClient, cfg: dict, threshold: float) -> tuple[list[dict], int, list[dict]]:
    """返回 (达标标的, 扫描数量, 全市场领涨榜)。"""
    valid = client.load_symbols()
    tickers = [t for t in client.fetch_tickers() if t["symbol"] in valid]
    candidates = filter_candidates(tickers, valid, cfg, threshold)
    movers = sorted(tickers, key=lambda x: x["pct"], reverse=True)[:3]
    return candidates, len(tickers), movers


def run(args: argparse.Namespace) -> int:
    cfg = prepare(args)
    setup_logger(bool(cfg.get("verbose", True)))
    if args.threshold is not None:
        cfg["threshold_percent"] = args.threshold
    if args.interval is not None:
        cfg["interval_seconds"] = args.interval

    threshold = float(cfg.get("threshold_percent", 120))
    interval = max(5, int(cfg.get("interval_seconds", 60)))
    cooldown = int(cfg.get("cooldown_seconds", 1800))
    reset_below = float(cfg.get("reset_below_percent", threshold - 20))
    if reset_below >= threshold:
        # 回落线必须低于触发线，否则会在阈值附近反复触发
        reset_below = max(0.0, threshold - max(5.0, threshold * 0.1))
        log.warning("reset_below_percent 不应高于阈值，已自动调整为 %.1f%%", reset_below)

    setup_logger(bool(cfg.get("verbose", True)))
    log.info("=" * 62)
    log.info("币安 U 本位合约监控启动 | 阈值 %.2f%% | 间隔 %ss | 冷却 %ss", threshold, interval, cooldown)
    log.info("配置文件：%s", Path(args.config).resolve())

    client = BinanceClient(cfg)
    notifier = Notifier(cfg)
    state = StateStore()

    if not client.ping():
        log.error("无法连接币安合约接口。")
        log.error("可能原因：当前网络被限制（DNS 污染 / IP 封锁），需要配置代理。")
        log.error("解决方案：在 config.json 的 \"proxy\" 填入本地代理地址，例如 http://127.0.0.1:7890")
        log.error("可先运行  python binance_monitor.py --check  进行环境自检")
        return 2

    log.info("行情节点：%s", client.base)

    if not args.dry_run and not notifier.is_ready():
        log.warning("尚未配置任何可用的推送通道，本次仅打印告警（不会真正推送）。")

    first_cycle = True
    stop = {"flag": False}

    try:
        while not stop["flag"]:
            started = time.time()
            try:
                candidates, scanned, movers = scan_once(client, cfg, threshold)
            except Exception as e:
                log.error("拉取行情失败：%s", e)
                client.base = None  # 下次重新选节点
                if not sleep_interruptible(interval, stop):
                    break
                continue

            valid = client.load_symbols()
            state.prune(valid)

            to_push: list[dict] = []
            for c in candidates:
                if first_cycle and not cfg.get("notify_on_start", True):
                    state.mark(c["symbol"], c["pct"])
                    continue
                if state.should_alert(c["symbol"], c["pct"], cooldown, reset_below):
                    to_push.append(c)

            if args.dry_run:
                to_push = candidates

            if to_push:
                title, md = build_message(to_push, threshold, scanned)
                log.warning("触发告警：%s", "、".join(f"{c['symbol']} +{c['pct']:.2f}%" for c in to_push))
                if args.dry_run:
                    print("\n" + plain_preview(title, md) + "\n", flush=True)
                else:
                    if notifier.is_ready():
                        notifier.send(title, md)
                    else:
                        print("\n" + plain_preview(title, md) + "\n", flush=True)
                    for c in to_push:
                        state.mark(c["symbol"], c["pct"])
                    state.save()

            top_txt = "、".join(f"{c['symbol']}+{c['pct']:.1f}%" for c in movers) or "无"
            log.info(
                "扫描 %d 个合约 | 达阈值 %d 个 | 本轮推送 %d 个 | 领涨：%s | 耗时 %.2fs",
                scanned, len(candidates), len(to_push), top_txt, time.time() - started,
            )
            first_cycle = False

            if args.once:
                break
            if not sleep_interruptible(interval, stop):
                break
    except KeyboardInterrupt:
        log.info("收到中断信号，正在退出 ...")
    finally:
        state.save()

    log.info("监控已停止。")
    return 0


def sleep_interruptible(seconds: int, stop: dict) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if stop["flag"]:
            return False
        time.sleep(min(1.0, max(0.0, end - time.time())))
    return True


# --------------------------------------------------------------------------- #
# 配置与自检
# --------------------------------------------------------------------------- #
def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"找不到配置文件：{p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"配置文件 JSON 格式错误：{e}")


def configure_paths(
    state_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
) -> None:
    """云端部署时把状态/缓存/日志集中到可持久化目录（如 GitHub Actions cache）。"""
    global STATE_PATH, SYMBOL_CACHE, LOG_DIR
    if state_path:
        STATE_PATH = Path(state_path).expanduser().resolve()
    if cache_dir:
        SYMBOL_CACHE = Path(cache_dir).expanduser().resolve() / ".symbols_cache.json"
    if log_dir:
        LOG_DIR = Path(log_dir).expanduser().resolve()


def apply_env_overrides(cfg: dict) -> dict:
    """支持用环境变量注入凭证与参数，避免把密钥写进代码仓库（CI / 云端必需）。"""
    env = os.environ

    def pick(*names: str) -> str:
        for n in names:
            v = (env.get(n) or "").strip()
            if v:
                return v
        return ""

    push = cfg.setdefault("push", {})

    v = pick("BM_THRESHOLD")
    if v:
        try:
            cfg["threshold_percent"] = float(v)
        except ValueError:
            log.warning("环境变量 BM_THRESHOLD 不是合法数字：%s", v)

    v = pick("BM_PROXY")
    if v:
        cfg["proxy"] = v

    v = pick("BM_BASE_URLS", "BINANCE_BASE_URLS")
    if v:
        cfg["base_urls"] = [x.strip() for x in v.split(",") if x.strip()]

    v = pick("WECOM_WEBHOOK", "BM_WECOM_WEBHOOK")
    if v:
        push["wecom_bot"] = {"enabled": True, "webhook_url": v}

    v = pick("SENDKEY", "BM_SENDKEY", "SERVERCHAN_KEY")
    if v:
        push["serverchan"] = {"enabled": True, "send_key": v}

    v = pick("PUSHPLUS_TOKEN", "BM_PUSHPLUS_TOKEN")
    if v:
        topic = ((push.get("pushplus") or {}).get("topic") or "")
        push["pushplus"] = {"enabled": True, "token": v, "topic": topic}

    return cfg


def prepare(args: argparse.Namespace) -> dict:
    """加载配置 -> 注入环境变量 -> 重定向状态路径。"""
    cfg = load_config(args.config)
    apply_env_overrides(cfg)
    configure_paths(
        getattr(args, "state_path", None) or cfg.get("state_path"),
        cfg.get("cache_dir"),
        cfg.get("log_dir"),
    )
    return cfg


def cmd_check(args: argparse.Namespace) -> int:
    cfg = prepare(args)
    setup_logger(True)

    print("=" * 62)
    print("环境自检")
    print("=" * 62)
    print(f"Python        : {sys.version.split()[0]}  ({sys.executable})")
    print(f"配置文件      : {Path(args.config).resolve()}")
    print(f"阈值          : {cfg.get('threshold_percent')}%")
    print(f"轮询间隔      : {cfg.get('interval_seconds')}s")
    print(f"最小成交额    : {cfg.get('min_quote_volume_usdt')} USDT")
    print("-" * 62)

    print("[1/3] 代理探测 ...")
    proxy = resolve_proxy(cfg)
    print(f"      => {proxy or '直连'}")
    if proxy:
        cfg["proxy"] = proxy  # 复用探测结果，避免重复探测

    print("[2/3] 币安合约接口 ...")
    client = BinanceClient(cfg)
    ok = client.ping()
    if ok:
        print(f"      => 连接正常，节点 {client.base}")
        try:
            tickers = client.fetch_tickers()
            print(f"      => 行情正常，返回 {len(tickers)} 个合约")
        except Exception as e:
            print(f"      => 行情拉取失败：{e}")
            ok = False
    else:
        print("      => 连接失败")
        print("      => 国内网络通常需要代理，请在 config.json 的 proxy 字段填写，")
        print("         例如 Clash: http://127.0.0.1:7890  /  v2rayN: http://127.0.0.1:10809")

    print("[3/3] 推送通道 ...")
    notifier = Notifier(cfg)
    channels = notifier.active_channels()
    if not channels:
        print("      => 未启用任何通道，请在 config.json -> push 中配置")
    for name in channels:
        ready = notifier._configured(name)
        print(f"      => {name:<15} {'已配置' if ready else '缺少凭证，已跳过'}")

    print("=" * 62)
    return 0 if ok else 2


def cmd_test_push(args: argparse.Namespace) -> int:
    cfg = prepare(args)
    setup_logger(True)
    notifier = Notifier(cfg)
    if not notifier.is_ready():
        print("未配置任何可用的推送通道，请先填写 config.json -> push")
        return 2

    title = "【测试】币安合约监控推送测试"
    md = (
        f"**测试时间**：{now_str()}\n\n"
        "如果你在微信里看到了这条消息，说明推送通道已经打通。\n\n"
        "**示例告警格式**：\n"
        "- 标的：ABCUSDT　+156.32%\n"
        "- 最新价：0.012340\n"
        "- 24H 成交额：1234.56 万 USDT\n"
    )
    ok = notifier.send(title, md)
    print("推送结果：", "成功" if ok else "失败（详见上方日志）")
    return 0 if ok else 1


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(
        description="币安 U 本位合约 24H 涨幅监控 + 微信推送",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--state-path", default=None, help="状态文件路径（云端部署用，如 state/state.json）")
    parser.add_argument("--once", action="store_true", help="只扫描一次后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印不推送、不写状态")
    parser.add_argument("--threshold", type=float, help="临时覆盖涨幅阈值（百分比）")
    parser.add_argument("--interval", type=int, help="临时覆盖轮询间隔（秒）")
    parser.add_argument("--check", action="store_true", help="环境自检：网络 / 代理 / 推送配置")
    parser.add_argument("--test-push", action="store_true", help="发送一条测试推送到微信")
    args = parser.parse_args()

    if args.check:
        return cmd_check(args)
    if args.test_push:
        return cmd_test_push(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
