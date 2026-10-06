"""WebUI 控制面板后端：供 AstrBot Dashboard 插件页面调用的数据与操作 API。

页面本体在 pages/dashboard/。Dashboard 把页面的桥接请求转发到
/api/v1/plugins/extensions/<插件名>/<endpoint>，本模块注册对应的路由：
只读的 overview 快照，加上与聊天指令同语义的增删操作和手动检查。
统计与最近动态的采集在 main.py 的业务路径里完成，这里只做展示与转发。

本模块不反向导入 main（AstrBot 以插件名为包名加载 main.py，from .main import
会把它再执行一遍，指令装饰器会重复注册）；main.py 需要分享的模块级工具
（uid 提取、代理打码）在注册时以参数传入。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from astrbot.api import logger

try:  # 新版 AstrBot：astrbot.api.web 提供请求代理与响应助手
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request

    _WEB_AVAILABLE = True
except ImportError:  # 旧版 Dashboard 走 Quart 原生上下文，做最小等价实现
    try:
        from quart import jsonify

        class web_request:  # noqa: N801 - 与新版的 request 代理同名，保持调用点一致
            """最小请求代理：面板只需要读 JSON body。"""

            @staticmethod
            async def json(default: Any = None) -> Any:
                from quart import request

                data = await request.get_json(silent=True)
                if isinstance(data, dict):
                    return data
                return {} if default is None else default

        def json_response(payload: dict):
            return jsonify(payload)

        def error_response(message: str, status_code: int = 400):
            resp = jsonify({"status": "error", "message": message})
            resp.status_code = status_code
            return resp

        _WEB_AVAILABLE = True
    except ImportError:
        # 连 quart 都没有的极简环境：面板 API 不可用，但不能拖垮插件本体加载
        _WEB_AVAILABLE = False

        def json_response(payload: dict):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        def error_response(message: str, status_code: int = 400):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        class web_request:  # noqa: N801
            @staticmethod
            async def json(default: Any = None) -> Any:
                return {} if default is None else default


# 面板单次返回的明细条数上限（队列与动态），全量数据在 state.json 里
PENDING_PAGE_LIMIT = 50
ACTIVITY_PAGE_LIMIT = 50


def register_dashboard(
    plugin,
    plugin_name: str,
    version: str,
    extract_uid: Callable[[str], str | None],
    redact_proxy: Callable[[str], str],
) -> bool:
    """把面板 API 挂到 Dashboard；AstrBot 不支持插件 Web API 时返回 False。"""
    context = getattr(plugin, "context", None)
    register = getattr(context, "register_web_api", None)
    if not callable(register) or not _WEB_AVAILABLE:
        return False
    api = DashboardAPI(plugin, plugin_name, version, extract_uid, redact_proxy)
    routes = (
        (f"/{plugin_name}/overview", api.overview, ["GET"], "微博转发面板总览"),
        (f"/{plugin_name}/check-now", api.check_now, ["POST"], "立即检查一轮"),
        (f"/{plugin_name}/accounts", api.accounts, ["POST"], "增删监控博主"),
        (f"/{plugin_name}/sessions", api.sessions, ["POST"], "增删推送目标"),
        (f"/{plugin_name}/album-rules", api.album_rules, ["POST"], "增删相册规则"),
    )
    ok = False
    for route, handler, methods, desc in routes:
        try:
            register(route, handler, methods, desc)
            ok = True
        except Exception as e:
            logger.warning(f"{plugin_name} 注册面板接口 {route} 失败: {e!r}")
    return ok


class DashboardAPI:
    """面板 API 的各 handler；持有插件实例，直接读它的运行时状态。"""

    def __init__(
        self,
        plugin,
        plugin_name: str,
        version: str,
        extract_uid: Callable[[str], str | None],
        redact_proxy: Callable[[str], str],
    ):
        self.p = plugin
        self.name = plugin_name
        self.version = version
        self._extract_uid = extract_uid
        self._redact_proxy = redact_proxy

    # ---------------- 工具 ----------------

    async def _body(self) -> dict:
        try:
            data = await web_request.json(default={})
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _now(self) -> float:
        return time.time()

    # ---------------- 只读总览 ----------------

    async def overview(self):
        p = self.p
        now = self._now()
        poll_interval = max(30, p._int_cfg("poll_interval", 120))
        risk_blocked = now < p._blocked_until
        identity_custom = bool(p._custom_cookies)

        accounts = []
        by_uid = p.stats.get("by_uid") or {}
        for uid, info in p.accounts.items():
            accounts.append(
                {
                    "uid": uid,
                    "name": str(info.get("name") or uid),
                    "seen_count": len(info.get("seen_ids") or []),
                    "baseline_done": bool(info.get("baseline_done")),
                    "fail_count": int(info.get("fail_count") or 0),
                    "next_retry_ts": float(info.get("next_retry_ts") or 0),
                    "push_ok": int((by_uid.get(uid) or {}).get("push_ok") or 0),
                }
            )

        rules = p._album_rules()
        album_rules = [
            {"uid": uid, "name": p._account_name(uid), "gid": gid, "album": album}
            for (uid, gid), album in rules.items()
        ]

        pending = [
            {
                "post_id": str(item.get("post_id") or ""),
                "uid": str(item.get("uid") or ""),
                "name": p._account_name(str(item.get("uid") or "")),
                "text": str(item.get("text") or "")[:120],
                "created_ts": float(item.get("created_ts") or 0),
                "retries": int(item.get("retries") or 0),
            }
            for item in p.pending[-PENDING_PAGE_LIMIT:]
        ]

        activity = list(p.activity)[-ACTIVITY_PAGE_LIMIT:]

        return json_response(
            {
                "version": self.version,
                "status": {
                    "poll_running": bool(p._poll_task and not p._poll_task.done()),
                    "checking": bool(p._checking),
                    "last_check_ts": float(p.last_check_ts or 0),
                    "next_check_ts": (
                        float(p.last_check_ts or 0) + poll_interval
                        if p.last_check_ts
                        else 0.0
                    ),
                    "poll_interval": poll_interval,
                    "pending_count": len(p.pending),
                    "pending_retrying": sum(
                        1 for item in p.pending if int(item.get("retries") or 0) > 0
                    ),
                    "risk_blocked": risk_blocked,
                    "risk_level": int(p._risk_level or 0),
                    "risk_until_ts": float(p._blocked_until or 0) if risk_blocked else 0.0,
                    "identity_mode": "custom" if identity_custom else "visitor",
                    "identity_cookie_keys": sorted(p._custom_cookies),
                    "visitor_ts": float(p._visitor_ts or 0),
                    "renew_fail_count": int(p._renew_fail_count or 0),
                    "proxy": self._redact_proxy(p._proxy) if p._proxy else "",
                    "ffmpeg": bool(p._ffmpeg),
                    "album_enabled": bool(p.config.get("album_enabled", True)),
                },
                "accounts": accounts,
                "sessions": p._sessions(),
                "album_rules": album_rules,
                "pending": pending,
                "stats": p.stats,
                "activity": activity,
            }
        )

    # ---------------- 操作 ----------------

    async def check_now(self):
        p = self.p
        if p._checking:
            return error_response("已有检查任务在进行中，请稍候", 409)
        if not p.accounts:
            return error_response("尚未监控任何账号，请先添加监控博主")
        if self._now() < p._blocked_until:
            remain = int((p._blocked_until - self._now()) // 60) + 1
            return error_response(f"微博风控冷却中（约 {remain} 分钟后自动恢复）", 409)
        if p._manual_check_task and not p._manual_check_task.done():
            return error_response("手动检查已在进行中", 409)

        async def _run():
            try:
                new_count = await p.check_all()
                p._record_event(
                    "info", detail=f"手动检查完成，检测到 {new_count} 条新微博"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{self.name} 手动检查失败: {e!r}")
                p._record_event("error", detail=f"手动检查失败: {e}")

        p._manual_check_task = asyncio.create_task(_run())
        return json_response({"started": True})

    async def accounts(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "add":
            uid = self._extract_uid(str(body.get("uid") or "")) or ""
            if not uid:
                return error_response("请输入有效的微博 uid 或主页链接")
            p._sync_accounts_from_config()
            if uid in p.accounts:
                return error_response(f"账号 {uid} 已在监控列表中")
            # 拉昵称会走真实的微博请求（带全局请求间隔），失败不阻塞添加
            nickname = ""
            try:
                nickname = await p._fetch_nickname(uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info(f"{self.name} 面板添加账号 {uid} 拉取昵称失败: {e!r}")
            p.accounts[uid] = {
                "name": nickname or uid,
                "seen_ids": [],
                "baseline_done": False,
                "fail_count": 0,
                "next_retry_ts": 0.0,
            }
            p.config["monitored_uids"] = list(p.accounts.keys())
            p.config.save_config()
            p._save_state()
            name = p._account_name(uid)
            p._record_event("info", uid=uid, name=name, detail="通过面板添加监控")
            return json_response({"uid": uid, "name": name, "nickname_ok": bool(nickname)})
        if action == "remove":
            uid = str(body.get("uid") or "").strip()
            p._sync_accounts_from_config()
            if uid not in p.accounts:
                return error_response(f"账号 {uid} 不在监控列表中")
            name = str(p.accounts.pop(uid).get("name") or uid)
            p.config["monitored_uids"] = list(p.accounts.keys())
            p.config.save_config()
            p._save_state()
            p._record_event("info", uid=uid, name=name, detail="通过面板取消监控")
            return json_response({"removed": uid, "name": name})
        return error_response("不支持的操作")

    async def sessions(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        umo = str(body.get("umo") or "").strip()
        if action == "add":
            parts = umo.split(":")
            if len(parts) != 3 or not all(parts):
                return error_response(
                    "会话 ID 格式应为 平台:消息类型:会话号，"
                    "例如 aiocqhttp:GroupMessage:123456"
                )
            sessions = p._sessions()
            if umo in sessions:
                return error_response("该会话已在推送列表中")
            sessions.append(umo)
            p.config["push_sessions"] = sessions
            p.config.save_config()
            p._record_event("info", detail=f"通过面板新增推送目标 {umo}")
            return json_response({"added": umo})
        if action == "remove":
            sessions = p._sessions()
            if umo not in sessions:
                return error_response("该会话不在推送列表中")
            sessions.remove(umo)
            p.config["push_sessions"] = sessions
            p.config.save_config()
            p._record_event("info", detail=f"通过面板移除推送目标 {umo}")
            return json_response({"removed": umo})
        return error_response("不支持的操作")

    async def album_rules(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "add":
            uid = self._extract_uid(str(body.get("uid") or "")) or ""
            gid = str(body.get("gid") or "").strip()
            album = str(body.get("album") or "").strip()
            if not uid or not gid or not album:
                return error_response("需要 uid、群号、相册名（或 ID）三项")
            if not gid.isdigit():
                return error_response("群号应为纯数字")
            p._sync_accounts_from_config()
            if uid not in p.accounts:
                return error_response(f"账号 {uid} 不在监控列表中，请先添加监控")
            rules = p._album_rules()
            rules[(uid, gid)] = album
            p.config["album_rules"] = [f"{u}:{g}:{a}" for (u, g), a in rules.items()]
            p.config.save_config()
            p._record_event(
                "info",
                uid=uid,
                name=p._account_name(uid),
                detail=f"面板绑定相册：群 {gid} 相册「{album}」",
            )
            return json_response({"uid": uid, "gid": gid, "album": album})
        if action == "remove":
            uid = str(body.get("uid") or "").strip()
            gid = str(body.get("gid") or "").strip()
            rules = p._album_rules()
            if (uid, gid) not in rules:
                return error_response("该规则不存在（可能已被移除）")
            rules.pop((uid, gid))
            p.config["album_rules"] = [f"{u}:{g}:{a}" for (u, g), a in rules.items()]
            p.config.save_config()
            p._record_event("info", uid=uid, detail=f"面板移除相册绑定：群 {gid}")
            return json_response({"removed": f"{uid}:{gid}"})
        return error_response("不支持的操作")
