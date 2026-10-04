from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import ipaddress
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_weibo_monitor"

WEIBO_HOME_URL = "https://m.weibo.cn/"
WEIBO_INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
WEIBO_EXTEND_URL = "https://m.weibo.cn/statuses/extend"
WEIBO_VISITOR_HOST = "https://visitor.passport.weibo.cn"
WEIBO_GENVISITOR_URL = f"{WEIBO_VISITOR_HOST}/visitor/genvisitor"
WEIBO_INCARNATE_URL = f"{WEIBO_VISITOR_HOST}/visitor/visitor"
# 游客系统入口页（genvisitor/incarnate 的 Referer）
WEIBO_VISITOR_REFERER = (
    f"{WEIBO_VISITOR_HOST}/visitor/visitor?entry=sinawap&a=enter&url="
    f"{quote(WEIBO_HOME_URL, safe='')}&domain=.weibo.cn&sudaref="
    "&ua=php-sso_sdk_client-0.6.36&_rand=1791030900.1"
)

# 出网白名单：微博时间线/全文接口 + 微博游客身份系统
WEIBO_ALLOWED_HOSTS = {"m.weibo.cn", "visitor.passport.weibo.cn"}

# UA 池：游客身份与 UA 绑定（一套身份一套指纹），全部为移动端 UA。
# 第一个为实测验证过的 UA。
UA_POOL = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
)
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.weibo.cn/",
    "MWeibo-Pwa": "1",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_MESSAGE_FORMAT = "📢 微博更新\n【{name}】{time}\n{weibo}\n🔗 {link}"

SEEN_IDS_LIMIT = 500
PENDING_HARD_LIMIT = 100
MIN_INTERVAL_SECONDS = 30
# show_full_weibo_text 开启时全文的安全上限（防超长文本刷屏/QQ 风控）
FULL_TEXT_MAX_CHARS = 2000
VISITOR_MAX_AGE = 3 * 24 * 3600   # 游客身份最长复用 3 天，到期主动换新
RENEW_COOLDOWN = 600.0            # 主动续期最小间隔（秒）：频繁 genvisitor 本身是风控信号
RENEW_MAX_PER_HOUR = 3            # 每小时最多续期尝试次数
RENEW_FAIL_BACKOFF = 600.0        # 续期失败退避基数（秒），指数递增，封顶 1 小时
REQUEST_GAP = (2.0, 5.0)          # 相邻两次微博请求的最小随机间隔（秒）
# HTTP 418/432 属于 IP 级风控：全局冷却梯度（秒），触发后暂停所有微博请求
RISK_COOLDOWN_STEPS = (600, 1800, 3600)
# 距上次风控触发超过该时长后冷却梯度重置，避免历史偶发触发导致永久 60 分钟冷却
RISK_ESCALATION_RESET = 2 * 3600

_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_TZ_CN = timezone(timedelta(hours=8))
_STD_TIME_RE = re.compile(
    r"^(\w{3}) (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) \+0800 (\d{4})$"
)
_JSONP_RE = re.compile(r"\((\{.*\})\)\s*;?\s*$")
_UID_PATTERNS = (
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.com/(\d+)/?"),
)

USAGE = (
    "微博监控指令：\n"
    "微博绑定 - 把当前群绑定为推送目标（管理员）\n"
    "微博解绑 - 解除当前群推送（管理员）\n"
    "微博添加 <uid或主页链接> - 添加监控账号（管理员）\n"
    "微博删除 <uid> - 取消监控（管理员）\n"
    "微博列表 - 查看监控账号与推送目标\n"
    "微博检测 - 立即检查一轮（管理员）\n"
    "微博状态 - 查看运行状态"
)


class WeiboFetchError(Exception):
    """微博接口请求/解析失败。"""


class WeiboAuthError(WeiboFetchError):
    """游客身份失效，需要重新换取后再试。"""


class WeiboRiskError(WeiboFetchError):
    """触发微博 IP 级风控（HTTP 418/432），需要全局冷却。"""


def _clean_text(raw: str, limit: int) -> str:
    """清洗 m.weibo.cn 返回的 HTML 富文本，截断到 limit。"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    # 微博表情等内嵌图片，保留 alt 文本（如 [笑cry]）
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _parse_created_epoch(raw: str) -> float:
    """把微博标准时间（Sat Mar 08 16:51:30 +0800 2025）解析为 Unix 时间戳。

    相对时间等无法解析的格式返回 0，由调用方采用宽松策略。
    """
    m = _STD_TIME_RE.match((raw or "").strip())
    if not m or m.group(2) not in _MONTH_MAP:
        return 0.0
    try:
        dt = datetime(
            int(m.group(7)), _MONTH_MAP[m.group(2)], int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=_TZ_CN,
        )
        return dt.timestamp()
    except ValueError:
        return 0.0


def _format_time(raw: str) -> str:
    """把接口时间转成可读字符串；相对时间（如“5分钟前”）原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _STD_TIME_RE.match(raw)
    if m and m.group(2) in _MONTH_MAP:
        year, mon, day = m.group(7), _MONTH_MAP[m.group(2)], int(m.group(3))
        ymd = f"{year}-{mon:02d}-{day:02d}"
        if year != str(datetime.now().year):
            return f"{ymd} {m.group(4)}:{m.group(5)}"
        return f"{mon:02d}-{day:02d} {m.group(4)}:{m.group(5)}"
    return raw


def _extract_uid(text: str) -> str | None:
    text = (text or "").strip()
    for pat in _UID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    if text.isdigit() and len(text) >= 4:
        return text
    return None


def _jsonp_data(body: str) -> dict[str, Any] | None:
    """解析 gen_callback({...}) / cross_domain({...}) 之类的 JSONP 响应。"""
    m = _JSONP_RE.search((body or "").strip())
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class WeiboMonitorPlugin(Star):
    """免 Cookie 监控微博账号更新，并转发到绑定的Q群。

    数据来源为 m.weibo.cn 匿名接口：插件自动向微博游客系统
    （visitor.passport.weibo.cn）换取匿名游客身份（SUB/SUBP 种在
    .weibo.cn 域），全程不需要用户提供任何微博 Cookie。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._http: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._checking = False
        self._data_dir: Path | None = None
        # 游客身份 cookie（仅 .weibo.cn 匿名游客，非用户账号）；UA 与身份绑定
        self._visitor_cookies: dict[str, str] = {}
        self._visitor_ts = 0.0
        self._ua: str = ""
        # 续期节奏控制
        self._last_renew_ts = 0.0
        self._renew_times: list[float] = []
        self._renew_fail_count = 0
        self._renew_fail_until = 0.0
        # IP 级风控全局冷却
        self._blocked_until = 0.0
        self._risk_level = 0
        self._last_risk_ts = 0.0
        # 全局请求间隔
        self._last_request_mono = 0.0
        self._proxy: str | None = None
        # 自定义 Cookie（config.custom_cookie，非空时优先于游客身份）
        self._custom_cookies: dict[str, str] = {}
        # 并发闸：请求间隔与游客续期在轮询任务与指令处理并发时同样全局生效
        self._pace_lock = asyncio.Lock()
        self._renew_lock = asyncio.Lock()
        # uid -> {"name", "seen_ids"(list), "baseline_done"}
        self.accounts: dict[str, dict[str, Any]] = {}
        # 待推送队列: [{"post_id","uid","text","created_ts","retries"}]
        self.pending: list[dict[str, Any]] = []
        self.last_check_ts = 0.0

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._load_state()
        self._sync_accounts_from_config()
        # DummyCookieJar：cookie 全部手工管理，避免与 jar 自动附带冲突
        self._http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            if proxy.startswith(("http://", "https://")):
                self._proxy = proxy
                logger.info(f"{PLUGIN_NAME} 使用代理出网: {proxy}")
            else:
                logger.warning(
                    f"{PLUGIN_NAME} proxy 配置无效（需 http:// 或 https:// 开头），已忽略: {proxy}"
                )
        # 自定义 Cookie 模式：直接使用用户身份，跳过游客身份的预热与续期
        self._custom_cookies = self._parse_cookie_string(
            str(self.config.get("custom_cookie") or "")
        )
        # 启动预热：身份过期则换新；身份健在则访问一次主页刷新 _T_WM/XSRF 指纹
        if self._custom_cookies:
            logger.info(
                f"{PLUGIN_NAME} 已启用自定义 Cookie（{sorted(self._custom_cookies)}），"
                "游客身份流程已跳过"
            )
        elif self._visitor_cookies.get("SUB") and (
            time.time() - self._visitor_ts <= VISITOR_MAX_AGE
        ):
            await self._warmup_quietly()
        else:
            await self._renew_visitor_quietly()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"{PLUGIN_NAME} 已启动：监控 {len(self.accounts)} 个账号，"
            f"推送目标 {len(self._sessions())} 个"
        )

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        self._save_state()
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None
        logger.info(f"{PLUGIN_NAME} 已停止")

    # ---------------- 配置与状态 ----------------

    def _sessions(self) -> list[str]:
        return [str(s) for s in (self.config.get("push_sessions") or [])]

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.config.get(key) or default))
        except (TypeError, ValueError):
            return default

    def _message_format(self) -> str:
        return str(self.config.get("message_format") or DEFAULT_MESSAGE_FORMAT).replace(
            "\\n", "\n"
        )

    def _max_post_age(self) -> float:
        """时效过滤上限（秒）；0 表示关闭。"""
        return float(self._int_cfg("max_post_age_minutes", 0) * 60)

    def _sync_accounts_from_config(self):
        """以配置里的 monitored_uids 为准同步监控列表（兼容 WebUI 直接改动）。"""
        uids: list[str] = []
        for item in self.config.get("monitored_uids") or []:
            uid = str(item).strip()
            if uid.isdigit():
                uids.append(uid)
        for uid in dict.fromkeys(uids):  # 去重且保持顺序
            if uid not in self.accounts:
                self.accounts[uid] = {
                    "name": uid,
                    "seen_ids": [],
                    "baseline_done": False,
                    "fail_count": 0,
                    "next_retry_ts": 0.0,
                }
        for uid in [u for u in self.accounts if u not in uids]:
            self.accounts.pop(uid, None)

    def _state_file(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / "state.json"

    def _load_state(self):
        try:
            raw = self._state_file().read_text(encoding="utf-8")
            state = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 读取 state.json 失败，将重建状态: {e}")
            return
        for uid, info in (state.get("accounts") or {}).items():
            if not str(uid).isdigit():
                continue
            seen = [str(i) for i in (info.get("seen_ids") or [])][-SEEN_IDS_LIMIT:]
            self.accounts[str(uid)] = {
                "name": str(info.get("name") or uid),
                "seen_ids": seen,
                "baseline_done": bool(info.get("baseline_done")),
                "fail_count": 0,
                "next_retry_ts": 0.0,
            }
        pending: list[dict[str, Any]] = []
        for item in state.get("pending") or []:
            if isinstance(item, dict) and item.get("post_id") and item.get("text"):
                item.setdefault("retries", 0)
                item.setdefault("created_ts", 0.0)
                pending.append(item)
        self.pending = pending[-PENDING_HARD_LIMIT:]
        self.last_check_ts = float(state.get("last_check_ts") or 0)
        visitor = state.get("visitor") or {}
        cookies = visitor.get("cookies") or {}
        if isinstance(cookies, dict) and cookies.get("SUB"):
            self._visitor_cookies = {str(k): str(v) for k, v in cookies.items()}
            self._visitor_ts = float(visitor.get("ts") or 0)
            ua = str(visitor.get("ua") or "")
            self._ua = ua if ua in UA_POOL else ""

    def _save_state(self):
        if self._data_dir is None:
            return
        state = {
            "version": 1,
            "accounts": {
                uid: {
                    "name": info["name"],
                    "seen_ids": info["seen_ids"][-SEEN_IDS_LIMIT:],
                    "baseline_done": info["baseline_done"],
                }
                for uid, info in self.accounts.items()
            },
            "pending": self.pending,
            "last_check_ts": self.last_check_ts,
            "visitor": {
                "cookies": self._visitor_cookies,
                "ts": self._visitor_ts,
                "ua": self._ua,
            },
        }
        try:
            self._state_file().write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 保存 state.json 失败: {e}")

    # ---------------- 网络请求 ----------------

    async def _validate_url(self, url: str):
        """出网前校验：仅 http/https、host 白名单、DNS 不解析到内网/保留地址。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WeiboFetchError(f"拒绝非 http(s) 协议: {parsed.scheme}")
        host = parsed.hostname or ""
        if host not in WEIBO_ALLOWED_HOSTS:
            raise WeiboFetchError(f"目标 host 不在白名单内: {host}")
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None)
        except OSError as e:
            raise WeiboFetchError(f"域名解析失败: {host} ({e})") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise WeiboFetchError(f"域名解析到受限地址，已拦截: {ip}")

    async def _pace(self):
        """全局请求间隔：任意两次微博请求之间至少间隔 REQUEST_GAP 内的随机秒数。

        加锁串行，保证轮询任务与指令处理并发时同样全局生效。
        """
        async with self._pace_lock:
            gap = random.uniform(*REQUEST_GAP)
            wait = self._last_request_mono + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_mono = time.monotonic()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._ua or UA_POOL[0],
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    async def _get_text(self, url: str, params: dict[str, Any],
                        headers: dict[str, str],
                        collect: dict[str, str] | None = None) -> str:
        await self._validate_url(url)
        await self._pace()
        assert self._http is not None
        timeout = aiohttp.ClientTimeout(total=15)
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with self._http.get(
                    url, params=params, headers=headers, timeout=timeout,
                    proxy=self._proxy,
                ) as resp:
                    if resp.status in (418, 432):
                        raise WeiboRiskError(f"HTTP {resp.status}，触发微博 IP 级风控")
                    resp.raise_for_status()
                    if collect is not None:
                        self._merge_cookies(collect, resp)
                    return await resp.text()
            except (WeiboRiskError, WeiboAuthError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
        raise WeiboFetchError(f"请求失败: {last_err}")

    async def _api_get(self, url: str, params: dict[str, Any],
                       referer: str | None = None) -> dict[str, Any]:
        """GET m.weibo.cn JSON 接口。

        ok=-100（游客身份过期）→ 换新身份重试一次；
        HTTP 418/432（IP 级风控）→ 记录全局冷却并换新身份，重试一次；
        冷却期内直接快速失败，避免冷却期间继续向微博出网。
        """
        if time.time() < self._blocked_until:
            raise WeiboFetchError(
                f"微博风控冷却中（剩余 {int(self._blocked_until - time.time())} 秒）"
            )
        for attempt in (1, 2):
            try:
                data = await self._api_get_once(url, params, referer)
                # 成功即解除风控冷却
                self._risk_level = 0
                self._blocked_until = 0.0
                return data
            except WeiboRiskError as e:
                self._mark_risk_blocked(e)
                if attempt == 2:
                    raise
                await self._renew_quietly_force()
            except WeiboAuthError as e:
                if attempt == 2:
                    raise
                logger.info(f"游客身份失效（{e}），正在自动换新…")
                await self._renew_visitor(force=True)
        raise WeiboFetchError("unreachable")  # pragma: no cover

    async def _api_get_once(self, url: str, params: dict[str, Any],
                            referer: str | None) -> dict[str, Any]:
        if self._http is None:
            raise WeiboFetchError("HTTP 会话未初始化")
        headers = self._base_headers()
        headers.update(API_HEADERS)
        headers["Referer"] = referer or "https://m.weibo.cn/"
        if self._custom_cookies:
            # 自定义 Cookie 模式：直接使用用户身份，不走游客续期
            cookies = self._custom_cookies
        else:
            if not self._visitor_cookies.get("SUB"):
                await self._renew_visitor()
            cookies = self._visitor_cookies
            xsrf = cookies.get("XSRF-TOKEN", "")
            if xsrf:
                headers["X-Xsrf-Token"] = xsrf
        headers["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in cookies.items()
        )
        body = await self._get_text(url, params, headers)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise WeiboFetchError("接口返回的不是 JSON") from e
        if not isinstance(data, dict):
            raise WeiboFetchError("接口返回异常")
        if data.get("ok") == -100:
            if self._custom_cookies:
                # 自定义 Cookie 失效时换新游客身份没有意义，直接报错由调用方回退
                raise WeiboFetchError("自定义 Cookie 已失效（接口要求登录）")
            raise WeiboAuthError("接口要求登录（ok=-100）")
        if data.get("ok") != 1:
            raise WeiboFetchError(f"接口返回异常 ok={data.get('ok')}")
        return data

    def _mark_risk_blocked(self, e: Exception):
        """IP 级风控：全局冷却并按连续触发次数升级（10 → 30 → 60 分钟）。

        冷却期内重复触发不叠加升级（同一轮请求的多次 432 只算一次）；
        距上次触发超过 RISK_ESCALATION_RESET 后梯度重置。
        """
        now = time.time()
        if now < self._blocked_until:
            return
        if now - self._last_risk_ts > RISK_ESCALATION_RESET:
            self._risk_level = 0
        self._last_risk_ts = now
        level = self._risk_level
        cooldown = RISK_COOLDOWN_STEPS[min(level, len(RISK_COOLDOWN_STEPS) - 1)]
        self._risk_level = min(level + 1, len(RISK_COOLDOWN_STEPS) - 1)
        self._blocked_until = now + cooldown
        logger.warning(
            f"{PLUGIN_NAME} 触发微博风控，全局冷却 {cooldown // 60} 分钟"
            f"（连续第 {level + 1} 次触发）: {e}"
        )

    async def _renew_quietly_force(self):
        try:
            await self._renew_visitor(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 换新游客身份失败: {e}")

    async def _renew_visitor_quietly(self):
        try:
            await self._renew_visitor()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 更新游客身份失败: {e}")

    async def _warmup_quietly(self):
        """启动预热：带现有身份访问一次主页，刷新 _T_WM/XSRF-TOKEN 等指纹 cookie。"""
        try:
            cookies: dict[str, str] = {}
            await self._get_text(WEIBO_HOME_URL, {}, self._base_headers(),
                                 collect=cookies)
            if cookies:
                self._visitor_cookies.update(cookies)
                self._save_state()
                logger.info(f"{PLUGIN_NAME} 启动预热完成（{sorted(cookies)}）")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 启动预热失败（不影响运行）: {e}")

    async def _renew_visitor(self, force: bool = False):
        """向微博游客系统换取新的匿名游客身份（SUB/SUBP，种在 .weibo.cn 域）。

        节流策略：每小时最多 RENEW_MAX_PER_HOUR 次尝试；
        失败后指数退避；非强制模式下受 RENEW_COOLDOWN 冷却限制。
        加锁串行，避免指令处理与轮询任务并发触发双重换新。
        """
        async with self._renew_lock:
            if self._http is None:
                raise WeiboFetchError("HTTP 会话未初始化")
            now = time.time()
            recent = [t for t in self._renew_times if now - t < 3600.0]
            self._renew_times = recent
            if len(recent) >= RENEW_MAX_PER_HOUR:
                raise WeiboFetchError(
                    f"游客身份续期已达每小时上限（{RENEW_MAX_PER_HOUR} 次），稍后自动重试"
                )
            if now < self._renew_fail_until:
                raise WeiboFetchError(
                    "上次续期失败，退避中："
                    f"{datetime.fromtimestamp(self._renew_fail_until).strftime('%H:%M:%S')} 后自动重试"
                )
            if (
                not force
                and self._visitor_cookies.get("SUB")
                and now - self._last_renew_ts < RENEW_COOLDOWN
            ):
                return  # 冷却期内且现有身份尚在，直接复用
            self._renew_times.append(now)
            self._last_renew_ts = now
            try:
                await self._do_renew()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._renew_fail_count += 1
                backoff = min(3600.0, RENEW_FAIL_BACKOFF * (2 ** (self._renew_fail_count - 1)))
                self._renew_fail_until = time.time() + backoff
                raise
            self._renew_fail_count = 0
            self._renew_fail_until = 0.0
            self._save_state()

    async def _do_renew(self):
        assert self._http is not None
        logger.info(f"{PLUGIN_NAME} 正在获取微博游客身份…")
        self._ua = random.choice(UA_POOL)
        cookies: dict[str, str] = {}
        common = self._base_headers()
        visitor_headers = dict(common)
        visitor_headers["Referer"] = WEIBO_VISITOR_REFERER

        # 1) 预热移动端主页，收集 _T_WM / XSRF-TOKEN / MLOGIN 等游客 cookie
        await self._get_text(WEIBO_HOME_URL, {}, common, collect=cookies)

        # 2) genvisitor 获取 tid
        fp = json.dumps(
            {
                "os": "1",
                "browser": "Chrome136,136,0,0",
                "fonts": "undefined",
                "screenInfo": "1920*1080*30",
                "plugins": "",
            },
            separators=(",", ":"),
        )
        body = await self._get_text(
            WEIBO_GENVISITOR_URL,
            {"cb": "gen_callback", "fp": quote(fp, safe="")},
            visitor_headers,
        )
        gen = _jsonp_data(body) or {}
        if gen.get("retcode") != 20000000:
            raise WeiboFetchError(f"genvisitor 失败: {gen.get('retcode')} {gen.get('msg')}")
        tid = str((gen.get("data") or {}).get("tid") or "")
        confidence = (gen.get("data") or {}).get("confidence", 90)
        if not tid:
            raise WeiboFetchError("genvisitor 未返回 tid")

        # 3) incarnate 换取游客 SUB/SUBP（响应 Set-Cookie 种在 .weibo.cn 域）
        inc_params = {
            "a": "incarnate",
            "t": tid,
            "w": "2",
            "c": str(confidence),
            "gc": "",
            "cb": "cross_domain",
            "from": "weibo",
            "_rand": f"{random.random():.17f}",
        }
        await self._get_text(WEIBO_INCARNATE_URL, inc_params, visitor_headers,
                             collect=cookies)

        if "SUB" not in cookies:
            raise WeiboAuthError("游客身份获取失败（未拿到 SUB）")
        cookies.setdefault("MLOGIN", "0")
        self._visitor_cookies = cookies
        self._visitor_ts = time.time()
        logger.info(f"{PLUGIN_NAME} 游客身份已更新（{sorted(cookies)}）")

    @staticmethod
    def _parse_cookie_string(raw: str) -> dict[str, str]:
        """把 "k1=v1; k2=v2" 形式的 Cookie 字符串解析为字典。"""
        cookies: dict[str, str] = {}
        for part in (raw or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = k.strip()
            v = v.strip()
            if k:
                cookies[k] = v
        return cookies

    @staticmethod
    def _merge_cookies(target: dict[str, str], resp: aiohttp.ClientResponse):
        """把响应 Set-Cookie 合入字典（仅保留 weibo 相关域）。"""
        for key, morsel in resp.cookies.items():
            domain = morsel.get("domain") or ""
            if domain and "weibo.cn" not in domain and "weibo.com" not in domain:
                continue
            target[str(key)] = str(morsel.value)

    # ---------------- 数据获取与解析 ----------------

    async def _apply_full_text(self, mb: dict[str, Any], post: dict[str, Any]):
        """show_full_weibo_text 开启时拉取超长微博全文，替换摘要。

        覆盖外层微博与转发原微博两处；任一获取失败安全回退摘要。
        已实测 /statuses/extend 游客身份可用（ok=1，data.longTextContent 为含 HTML 的全文）。
        """
        status_id = str(mb.get("id") or mb.get("mid") or "")
        if status_id and mb.get("isLongText"):
            full = await self._fetch_long_text(status_id)
            if full:
                post["text"] = full
        if post["is_retweet"] and post.get("orig_id") and post.get("orig_is_long"):
            full = await self._fetch_long_text(str(post["orig_id"]))
            if full:
                post["orig_text"] = full

    async def _fetch_long_text(self, status_id: str) -> str | None:
        """拉取超长微博全文，失败返回 None（调用方回退摘要）。"""
        try:
            data = await self._api_get(
                WEIBO_EXTEND_URL,
                {"id": status_id},
                referer=f"https://m.weibo.cn/detail/{status_id}",
            )
            long_text = (data.get("data") or {}).get("longTextContent")
            if isinstance(long_text, str) and long_text.strip():
                # 全文用独立的安全上限，不受摘要 text_max_length 影响
                return _clean_text(long_text, FULL_TEXT_MAX_CHARS)
            logger.info(f"长微博 {status_id} 全文响应缺少正文，使用摘要")
        except asyncio.CancelledError:
            raise
        except WeiboFetchError as e:
            logger.info(f"获取长微博 {status_id} 全文失败，使用摘要: {e}")
        return None

    async def _fetch_timeline(self, uid: str) -> list[dict[str, Any]]:
        """拉取用户最新一页微博，返回解析后的帖子列表（新→旧）。"""
        data = await self._api_get(
            WEIBO_INDEX_URL,
            {
                "type": "uid",
                "value": uid,
                "containerid": f"107603{uid}",
                "page": 1,
                "count": 20,
            },
            referer=f"https://m.weibo.cn/u/{uid}",
        )
        cards = (data.get("data") or {}).get("cards") or []
        show_full = bool(self.config.get("show_full_weibo_text", False))
        posts: list[dict[str, Any]] = []
        for card in cards:
            if card.get("card_type") != 9:
                continue
            mb = card.get("mblog") or {}
            if self._is_pinned(card, mb):
                continue
            post = self._parse_post(mb, uid)
            if not post:
                continue
            if show_full:
                await self._apply_full_text(mb, post)
            posts.append(post)
        return posts

    async def _fetch_nickname(self, uid: str) -> str | None:
        """通过用户资料容器拉取昵称，失败返回 None。"""
        try:
            data = await self._api_get(
                WEIBO_INDEX_URL,
                {"type": "uid", "value": uid, "containerid": f"100505{uid}"},
                referer=f"https://m.weibo.cn/u/{uid}",
            )
            info = (data.get("data") or {}).get("userInfo") or {}
            name = str(info.get("screen_name") or "").strip()
            if name:
                return name
        except WeiboFetchError as e:
            logger.info(f"拉取用户 {uid} 昵称失败（不影响添加）: {e}")
        return None

    @staticmethod
    def _is_pinned(card: dict[str, Any], mb: dict[str, Any]) -> bool:
        for node in (card, mb):
            with contextlib.suppress(TypeError, ValueError):
                if int(node.get("mblogtype") or 0) == 2:
                    return True
            if node.get("isTop") or node.get("is_top"):
                return True
        title = mb.get("title")
        if isinstance(title, dict) and title.get("text") == "置顶":
            return True
        return False

    def _parse_post(self, mb: dict[str, Any], fallback_uid: str) -> dict[str, Any] | None:
        pid = str(mb.get("id") or mb.get("mid") or "").strip()
        if not pid:
            return None
        user = mb.get("user") or {}
        uid = str(user.get("id") or fallback_uid)
        bid = str(mb.get("bid") or pid)
        limit = self._int_cfg("text_max_length", 100)
        created_raw = str(mb.get("created_at") or "")

        own_text = _clean_text(str(mb.get("text") or ""), limit)
        orig_name, orig_text, orig_id = "", "", ""
        orig_is_long = False
        is_retweet = "retweeted_status" in mb
        if is_retweet:
            rt = mb.get("retweeted_status") or {}
            rt_user = rt.get("user") or {}
            orig_name = str(rt_user.get("screen_name") or "")
            orig_text = _clean_text(str(rt.get("text") or ""), limit)
            orig_id = str(rt.get("id") or rt.get("mid") or "")
            orig_is_long = bool(rt.get("isLongText"))

        return {
            "id": pid,
            "uid": uid,
            "author": str(user.get("screen_name") or "") or uid,
            "link": f"https://weibo.com/{uid}/{bid}",
            "time": _format_time(created_raw),
            "created_ts": _parse_created_epoch(created_raw),
            "text": own_text,
            "is_retweet": is_retweet,
            "orig_name": orig_name,
            "orig_text": orig_text,
            "orig_id": orig_id,
            "orig_is_long": orig_is_long,
        }

    def _post_body(self, post: dict[str, Any]) -> str:
        """模板 {weibo} 占位符的正文内容。"""
        if post["is_retweet"]:
            parts: list[str] = []
            if post["text"]:
                parts.append(post["text"])
            who = post["orig_name"] or "原作者"
            parts.append(f"转发 @{who}：{post['orig_text'] or '（原微博内容不可见）'}")
            return "\n".join(parts)
        return post["text"] or ""

    def _keyword_hit(self, post: dict[str, Any]) -> str | None:
        """关键词过滤：命中屏蔽词或被白名单排除时返回原因，否则 None。"""
        text = f"{post['text']}\n{post['orig_text']}"
        for kw in self.config.get("filter_keywords") or []:
            kw = str(kw).strip()
            if kw and kw in text:
                return f"包含屏蔽词“{kw}”"
        whitelist = [str(k).strip() for k in (self.config.get("whitelist_keywords") or [])
                     if str(k).strip()]
        if whitelist and not any(kw in text for kw in whitelist):
            return "未包含任何白名单关键词"
        return None

    def _build_message(self, post: dict[str, Any]) -> str:
        name = self.accounts.get(post["uid"], {}).get("name") or post["author"]
        msg = (
            self._message_format()
            .replace("{name}", name)
            .replace("{time}", post["time"])
            .replace("{weibo}", self._post_body(post))
            .replace("{link}", post["link"])
        )
        return re.sub(r"\n{3,}", "\n\n", msg).strip()

    # ---------------- 轮询与推送 ----------------

    async def _poll_loop(self):
        logger.info(f"{PLUGIN_NAME} 轮询任务已启动")
        await asyncio.sleep(5)
        while True:
            interval = max(MIN_INTERVAL_SECONDS, self._int_cfg("poll_interval", 120))
            try:
                pushed = await self.check_all()
                if pushed:
                    logger.info(f"本轮共检测到 {pushed} 条新微博")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"{PLUGIN_NAME} 轮询异常: {e!r}")
            jitter = random.uniform(0, min(15, interval * 0.2))
            if time.time() < self._blocked_until:
                # 风控冷却期间用短周期轮询，冷却一结束立即恢复检查
                await asyncio.sleep(min(60, interval))
            else:
                await asyncio.sleep(interval + jitter)

    def _is_expired(self, created_ts: float, at_ts: float | None = None) -> bool:
        """时效过滤：超过 max_post_age_minutes 的微博视为过期。

        发布时间无法解析（created_ts=0）时宽松放行。
        """
        max_age = self._max_post_age()
        if max_age <= 0 or not created_ts:
            return False
        return (at_ts or time.time()) - created_ts > max_age

    async def check_all(self) -> int:
        """检查一轮所有账号并推送，返回本轮新检测到的微博数。"""
        if self._checking:
            return 0
        if time.time() < self._blocked_until:
            logger.info(f"{PLUGIN_NAME} 风控冷却中，本轮跳过")
            return 0
        self._checking = True
        try:
            self._sync_accounts_from_config()
            if not self._custom_cookies and (
                not self._visitor_cookies.get("SUB")
                or time.time() - self._visitor_ts > VISITOR_MAX_AGE
            ):
                await self._renew_visitor_quietly()
            new_count = 0
            for uid, info in list(self.accounts.items()):
                if time.time() < self._blocked_until:
                    logger.warning(f"{PLUGIN_NAME} 风控冷却生效，本轮剩余账号跳过")
                    break
                if time.time() < info.get("next_retry_ts", 0):
                    continue
                try:
                    new_count += await self._check_account(uid)
                    info["fail_count"] = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    info["fail_count"] = info.get("fail_count", 0) + 1
                    fail = info["fail_count"]
                    backoff = min(600.0, 60.0 * 2 ** (fail - 1)) if fail >= 2 else 60.0
                    info["next_retry_ts"] = time.time() + backoff
                    logger.warning(
                        f"检查账号 {uid} 失败（连续第 {fail} 次，{backoff:.0f}s 后重试）: {e}"
                    )
                await asyncio.sleep(random.uniform(1.0, 3.0))
            await self._flush_pending()
            self.last_check_ts = time.time()
            self._save_state()
            return new_count
        finally:
            self._checking = False

    async def _check_account(self, uid: str) -> int:
        info = self.accounts[uid]
        posts = await self._fetch_timeline(uid)
        # 用接口数据修正昵称（添加时可能没拉到）
        if posts and info.get("name") == uid:
            info["name"] = posts[0]["author"]
        seen: set[str] = set(info.get("seen_ids") or [])
        if not info.get("baseline_done"):
            # 首次观察：只记录基线，不推送历史微博
            for post in posts:
                seen.add(post["id"])
            info["seen_ids"] = list(seen)[-SEEN_IDS_LIMIT:]
            info["baseline_done"] = True
            logger.info(f"账号 {uid} 基线已建立，共 {len(posts)} 条历史微博")
            return 0
        new_posts = [p for p in posts if p["id"] not in seen]
        if not new_posts:
            return 0
        include_rt = bool(self.config.get("include_retweets", True))
        for post in reversed(new_posts):  # 旧→新依次入队
            seen.add(post["id"])
            if self._is_expired(post.get("created_ts", 0.0)):
                continue
            if post["is_retweet"] and not include_rt:
                continue
            hit = self._keyword_hit(post)
            if hit:
                logger.info(f"微博 {post['id']} {hit}，已跳过推送")
                continue
            if len(self.pending) >= PENDING_HARD_LIMIT:
                logger.warning("待推送队列已满，丢弃更早的新微博")
                continue
            self.pending.append(
                {
                    "post_id": post["id"],
                    "uid": post["uid"],
                    "text": self._build_message(post),
                    "created_ts": post.get("created_ts", 0.0),
                    "retries": 0,
                }
            )
        info["seen_ids"] = list(seen)[-SEEN_IDS_LIMIT:]
        return len(new_posts)

    async def _send_with_timeout(self, session: str, text: str) -> bool:
        """带超时的主动消息发送，防止单个适配器卡死拖住整个轮询任务。"""
        send_timeout = self._int_cfg("message_send_timeout", 60)
        try:
            if send_timeout > 0:
                ok = await asyncio.wait_for(
                    self.context.send_message(session, MessageChain().message(text)),
                    timeout=send_timeout,
                )
            else:
                ok = await self.context.send_message(
                    session, MessageChain().message(text)
                )
            if ok is False:
                raise RuntimeError("AstrBot 未找到匹配的消息平台")
            return True
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"发送超过 {send_timeout} 秒，平台是否已接收未知") from e

    async def _flush_pending(self):
        """把待推送队列发送到所有绑定的会话。"""
        max_retries = self._int_cfg("max_pending_retries", 20)
        delay = self._int_cfg("push_delay_seconds", 2)
        sessions = self._sessions()
        total = len(self.pending)
        remaining: list[dict[str, Any]] = []
        for idx, item in enumerate(self.pending):
            if self._is_expired(item.get("created_ts", 0.0)):
                logger.info(f"待推送微博 {item['post_id']} 已超过时效上限，自动清除")
                continue
            if not sessions:
                # 未绑定推送目标：保留消息但不消耗重试次数
                remaining.append(item)
                continue
            if item.get("retries", 0) >= max_retries:
                logger.warning(f"推送重试超限，放弃微博 {item['post_id']}")
                continue
            sent = False
            for session in sessions:
                try:
                    if await self._send_with_timeout(session, item["text"]):
                        sent = True
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"推送到 {session} 失败: {e!r}")
            if sent:
                logger.info(f"已推送微博 {item['post_id']}")
            else:
                item["retries"] = item.get("retries", 0) + 1
                remaining.append(item)
            # 多条之间留间隔防刷屏；最后一条之后无需再等
            if delay and idx < total - 1:
                await asyncio.sleep(delay)
        if not sessions and len(remaining) > PENDING_HARD_LIMIT:
            remaining = remaining[-PENDING_HARD_LIMIT:]
        self.pending = remaining

    # ---------------- 指令 ----------------

    @filter.command_group("微博")
    async def weibo(self, event: AstrMessageEvent):
        """微博监控转发指令组"""
        yield event.plain_result(USAGE)

    @weibo.command("帮助", alias={"help", "用法"})
    async def weibo_help(self, event: AstrMessageEvent):
        """查看微博监控指令用法"""
        yield event.plain_result(USAGE)

    @weibo.command("绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_bind(self, event: AstrMessageEvent):
        """把当前群聊绑定为微博推送目标（管理员）"""
        if not event.get_group_id():
            yield event.plain_result("请在要接收推送的群聊中使用该指令")
            return
        umo = event.unified_msg_origin
        sessions = self._sessions()
        if umo in sessions:
            yield event.plain_result("本群已在推送列表中，无需重复绑定")
            return
        sessions.append(umo)
        self.config["push_sessions"] = sessions
        self.config.save_config()
        logger.info(f"{PLUGIN_NAME} 新增推送目标: {umo}")
        yield event.plain_result("绑定成功，本群将接收微博更新推送")

    @weibo.command("解绑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_unbind(self, event: AstrMessageEvent):
        """解除当前群聊的微博推送（管理员）"""
        umo = event.unified_msg_origin
        sessions = self._sessions()
        if umo not in sessions:
            yield event.plain_result("本群尚未绑定推送")
            return
        sessions.remove(umo)
        self.config["push_sessions"] = sessions
        self.config.save_config()
        logger.info(f"{PLUGIN_NAME} 移除推送目标: {umo}")
        yield event.plain_result("解绑成功，本群不再接收推送")

    @weibo.command("添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_add(self, event: AstrMessageEvent, uid_text: str = ""):
        """添加监控的微博账号，参数为 uid 或主页链接（管理员）"""
        uid = _extract_uid(uid_text)
        if not uid:
            yield event.plain_result(
                "用法：微博添加 <uid或主页链接>\n"
                "例如：微博添加 1195230310 或 微博添加 https://weibo.com/u/1195230310"
            )
            return
        self._sync_accounts_from_config()
        if uid in self.accounts:
            yield event.plain_result(f"账号 {uid} 已在监控列表中")
            return
        yield event.plain_result("正在验证账号…")
        nickname = await self._fetch_nickname(uid)
        self.accounts[uid] = {
            "name": nickname or uid,
            "seen_ids": [],
            "baseline_done": False,
            "fail_count": 0,
            "next_retry_ts": 0.0,
        }
        self.config["monitored_uids"] = list(self.accounts.keys())
        self.config.save_config()
        self._save_state()
        name = self.accounts[uid]["name"]
        extra = "" if nickname else "（未能获取昵称，下轮检查会自动补齐，请留意确认 uid 正确）"
        yield event.plain_result(
            f"已添加监控：{name}（{uid}）{extra}\n"
            "下一轮检查将建立基线，历史微博不会推送，仅推送之后的新微博。"
        )

    @weibo.command("删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_remove(self, event: AstrMessageEvent, uid_text: str = ""):
        """取消监控某个微博账号（管理员）"""
        uid = _extract_uid(uid_text)
        self._sync_accounts_from_config()
        if not uid or uid not in self.accounts:
            yield event.plain_result(
                f"账号 {uid or '(空)'} 不在监控列表中，请用 微博列表 查看"
            )
            return
        name = self.accounts.pop(uid)["name"]
        self.config["monitored_uids"] = list(self.accounts.keys())
        self.config.save_config()
        self._save_state()
        yield event.plain_result(f"已取消监控：{name}（{uid}）")

    @weibo.command("列表")
    async def weibo_list(self, event: AstrMessageEvent):
        """查看当前监控的微博账号"""
        self._sync_accounts_from_config()
        if not self.accounts:
            yield event.plain_result("尚未监控任何账号，管理员可用 微博添加 <uid> 添加")
            return
        lines = ["当前监控的微博账号："]
        for idx, (uid, info) in enumerate(self.accounts.items(), 1):
            lines.append(f"{idx}. {info['name']}（{uid}）")
        sessions = self._sessions()
        lines.append(f"推送目标：{len(sessions)} 个（群内发送 微博绑定 添加）")
        yield event.plain_result("\n".join(lines))

    @weibo.command("检测")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_check(self, event: AstrMessageEvent):
        """立即检查一轮微博更新（管理员）"""
        if self._checking:
            yield event.plain_result("已有检查任务在进行中，请稍候再试")
            return
        if not self.accounts:
            yield event.plain_result("尚未监控任何账号，请先用 微博添加 添加")
            return
        if time.time() < self._blocked_until:
            remain = int((self._blocked_until - time.time()) // 60) + 1
            yield event.plain_result(
                f"微博风控冷却中（约 {remain} 分钟后自动恢复），请稍后再试"
            )
            return
        yield event.plain_result("正在检查微博更新…")
        new_count = await self.check_all()
        if new_count:
            yield event.plain_result(f"检查完成，本轮检测到 {new_count} 条新微博")
        else:
            yield event.plain_result("检查完成，暂无新微博")

    @weibo.command("状态")
    async def weibo_status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
        lines = ["微博监控运行状态："]
        last = (
            datetime.fromtimestamp(self.last_check_ts).strftime("%Y-%m-%d %H:%M:%S")
            if self.last_check_ts
            else "尚未检查"
        )
        lines.append(f"上次检查：{last}")
        lines.append(f"监控账号：{len(self.accounts)} 个")
        lines.append(f"推送目标：{len(self._sessions())} 个")
        lines.append(f"待推送/重试：{len(self.pending)} 条")
        if time.time() < self._blocked_until:
            remain = int((self._blocked_until - time.time()) // 60) + 1
            lines.append(f"风控状态：冷却中（约 {remain} 分钟后恢复）")
        else:
            lines.append("风控状态：正常")
        if self._visitor_ts:
            age = (time.time() - self._visitor_ts) / 3600
            lines.append(f"游客身份：{age:.1f} 小时前更新")
        else:
            lines.append("游客身份：未获取")
        if self._custom_cookies:
            lines.append(f"身份模式：自定义 Cookie（{sorted(self._custom_cookies)}）")
        else:
            lines.append("身份模式：免 Cookie 游客身份")
        lines.append(f"出网方式：{'代理 ' + self._proxy if self._proxy else '直连'}")
        yield event.plain_result("\n".join(lines))
