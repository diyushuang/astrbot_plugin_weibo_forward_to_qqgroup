from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html as html_lib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Video
from astrbot.api.star import Context, Star, StarTools

try:
    # 与 Video.to_dict() 读的是同一个全局配置对象，用它探测 callback_api_base
    # 才能和 AstrBot 出站行为保持一致
    from astrbot.core import astrbot_config
except ImportError:  # 独立脚本 / 基准测试环境
    astrbot_config = None

from .napcat_album import (
    ALBUM_LIST_ITEM_ID_KEYS,
    ALBUM_LIST_ITEM_NAME_KEYS,
    MEDIA_NAME_KEYS,
    NapCatAlbum,
    NapCatError,
    pick,
)
from .dashboard import register_dashboard

PLUGIN_NAME = "astrbot_plugin_weibo_forward_to_qqgroup"
PLUGIN_VERSION = "v1.6.1"

WEIBO_HOME_URL = "https://m.weibo.cn/"
WEIBO_INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
WEIBO_EXTEND_URL = "https://m.weibo.cn/statuses/extend"
WEIBO_SHOW_URL = "https://m.weibo.cn/statuses/show"
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

# 相册/视频媒体下载白名单（与 API 白名单分离）：图片 CDN + 视频段/视频 CDN。
# .sina.com.cn 覆盖旧版视频 CDN（*.video.sina.com.cn）
MEDIA_ALLOWED_SUFFIXES = (
    ".sinaimg.cn",
    ".weibocdn.com",
    ".weibocdn.me",
    ".weibo.com",
    ".weibo.cn",
    ".sina.com.cn",
)

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
# 批量推送期间 state.json 的落盘合并间隔（秒）：state.json 是全量序列化
# （seen_ids×账号数 + 动态 + 队列），逐条落盘是纯写放大。首批照常立即落盘，
# 崩溃安全窗口最多放宽一个间隔
STATE_SAVE_DEBOUNCE = 10.0
# 时间线翻页补漏上限（共 3 页 = 60 条）：两次检查之间积压超过一页时向后翻，
# 再多说明轮询配置与博主的发博频率严重不匹配，翻页也补不完，交给时效过滤
TIMELINE_MAX_PAGES = 3
# show_full_weibo_text 开启时全文的安全上限（防超长文本刷屏/QQ 风控）
FULL_TEXT_MAX_CHARS = 2000
VISITOR_MAX_AGE = 3 * 24 * 3600  # 游客身份最长复用 3 天，到期主动换新
RENEW_COOLDOWN = 600.0  # 主动续期最小间隔（秒）：频繁 genvisitor 本身是风控信号
RENEW_MAX_PER_HOUR = 3  # 每小时最多续期尝试次数
RENEW_FAIL_BACKOFF = 600.0  # 续期失败退避基数（秒），指数递增，封顶 1 小时
REQUEST_GAP = (2.0, 5.0)  # 相邻两次微博请求的最小随机间隔（秒）
# 出网校验用 DNS 解析缓存时长（秒）：白名单 host 只有寥寥几个且极少变更，
# 而每个接口请求、每张图片下载前都要 getaddrinfo 一遍
DNS_CACHE_TTL = 300.0
# HTTP 418/432 属于 IP 级风控：全局冷却梯度（秒），触发后暂停所有微博请求
RISK_COOLDOWN_STEPS = (600, 1800, 3600)
# 距上次风控触发超过该时长后冷却梯度重置，避免历史偶发触发导致永久 60 分钟冷却
RISK_ESCALATION_RESET = 2 * 3600

# ---------------- 群相册自动上传 ----------------
DEFAULT_MAX_IMAGES = 9  # 单条微博最多处理的图片数（消息带图与相册上传共用上限）
IMG_MAX_BYTES = 30 * 1024 * 1024  # 单张图片/实况图视频段的下载体积上限
DISK_FLOOR_MB = 1024  # 批次下载前要求临时目录所在盘至少剩余空间（MB），防写满磁盘
ALBUM_TMP_DIR = "album_tmp"  # 相册上传的临时下载目录（插件数据目录下），传完即删
ALBUM_RESOLVE_TTL = 3600.0  # (群, 相册) 解析结果的内存缓存时长（秒）
LEDGER_TTL = 30 * 86400  # "这张图传过"台账保留时长（与上游相册插件口径一致）
LEDGER_MAX = 2000  # 每个相册最多记多少条台账
BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\s]+')
# 诊断文件：每批相册下载/上传在重负载动作"之前"写入体量与配置快照并立即刷盘。
# 整机被内存顶死时主日志往往来不及落盘，这份文件重启后还能还原死机前最后几批
# 的状态（与 astrbot_plugin_weibo_album 的 diagnostic.log 同一套做法）
DIAG_FILE = "diagnostic.log"
DIAG_MAX_BYTES = 512 * 1024  # 诊断文件轮转阈值：超限只留后半段
GIF_MAX_BYTES = 30 * 1024 * 1024  # 实况图 GIF 产物上限：超限回落封面静图，防超大 GIF
SINAIMG_RE = re.compile(
    r"(https?:)?//([a-z0-9]+)\.sinaimg\.cn/([a-z0-9]+)/([0-9a-zA-Z]+)\.(\w+)", re.I
)

# ---------------- 视频转发 ----------------
VIDEO_TMP_DIR = "video_tmp"  # 视频推送的临时下载目录（插件数据目录下），发完即删
VIDEO_MAX_MB_DEFAULT = 95  # 单视频下载/发送体积上限默认值：QQ 视频消息硬限 100MB，留出余量
VIDEO_DL_TIMEOUT = 300.0  # 单个视频下载总超时（秒）：几十上百 MB 的文件比图片慢得多
# 含视频消息的发送超时下限：视频上传 QQ 比九张图还慢，沿用图片的 60s 常常
# "实际已发出但被判超时"，重试会让群里重复出现同一条视频
VIDEO_SEND_TIMEOUT_FLOOR = 180
VIDEO_FALLBACK_NOTE = "🎬 视频未能自动转发，请点原帖链接观看"
VIDEO_PARTIAL_NOTE = "🎬 部分视频未能自动转发，请点原帖链接观看"

# ---------------- WebUI 控制面板（dashboard.py 提供页面与 API） ----------------
ACTIVITY_MAX = 200  # 最近动态最多保留条数（随 state.json 持久化）
ACTIVITY_TEXT_MAX = 80  # 单条动态摘要截断长度，防 state.json 膨胀
STATS_DAILY_KEEP = 14  # 按天推送统计桶保留天数

_MONTH_MAP = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
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
    "微博相册绑定 <uid> <相册名或ID> - 该博主的微博图片自动上传到本群相册（管理员）\n"
    "微博相册移除 <uid> - 解除博主的相册绑定（管理员）\n"
    "微博相册列表 - 查看所有相册绑定规则\n"
    "微博列相册 - 查看本群的相册列表（管理员）\n"
    "微博检测 - 立即检查一轮（管理员）\n"
    "微博状态 - 查看运行状态\n"
    "WebUI 控制面板：AstrBot 管理面板 - 插件 - 本插件详情页打开"
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
            int(m.group(7)),
            _MONTH_MAP[m.group(2)],
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6)),
            tzinfo=_TZ_CN,
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
        if year != str(datetime.now(tz=_TZ_CN).year):
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


def _redact_proxy(url: str) -> str:
    """抹掉代理里的 user:pass@，proxy 可能带凭据而 微博状态 不做权限校验。"""
    parsed = urlparse(url)
    if not (parsed.username or parsed.password):
        return url
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(parsed._replace(netloc=f"***@{host}"))


def _jsonp_data(body: str) -> dict[str, Any] | None:
    """解析 gen_callback({...}) / cross_domain({...}) 之类的 JSONP 响应。"""
    m = _JSONP_RE.search((body or "").strip())
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def to_original(url: str) -> str:
    """把任意 sinaimg 缩略图地址改写成 /large/ 原图地址。"""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    m = SINAIMG_RE.search(url)
    if not m:
        return url
    scheme = url[: m.start()] if url[: m.start()].endswith("://") else "https://"
    token, pid, ext = m.group(3), m.group(4), m.group(5)
    if token in ("large", "original"):
        return url
    return f"{scheme}{m.group(2)}.sinaimg.cn/large/{pid}.{ext}"


def to_light(url: str) -> str:
    """把任意 sinaimg 地址改写成 /mw2000/ 轻量档（长边 2000px），仅供推送消息带图。

    消息里的图是 NapCat 自己去 sina CDN 抓的：/large/ 原图单张 5~30MB，九张串在
    一条消息里很容易顶穿 message_send_timeout，超时又按"发送失败"进重试，结果同一条
    微博在群里重复出现。mw2000 是微博 CDN 原生档位，改一个路径段就有，不需要本地
    下载也不需要压缩。群相册那条仍然用 /large/ 原图，本函数不影响它。
    """
    url = (url or "").strip()
    m = SINAIMG_RE.search(url)
    if not m:
        return url
    return f"https://{m.group(2)}.sinaimg.cn/mw2000/{m.group(4)}.{m.group(5)}"


def _pic_nodes(mb: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容两种结构：m.weibo.cn 的 mblog.pics 与桌面端 ajax 的 pic_infos。"""
    pics = mb.get("pics")
    if isinstance(pics, list) and pics:
        return [p for p in pics if isinstance(p, dict)]
    infos = mb.get("pic_infos")
    if isinstance(infos, dict) and infos:
        out: list[dict[str, Any]] = []
        for pid in mb.get("pic_ids") or list(infos):
            entry = infos.get(pid)
            if isinstance(entry, dict):
                merged = dict(entry)
                merged.setdefault("pid", pid)
                out.append(merged)
        return out
    return []


def _abs_https(url: str) -> str:
    """把接口里的协议相对地址（//host/...）与 http 地址统一成 https。"""
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _video_candidates(mi: dict[str, Any], prefer_hd: bool) -> list[str]:
    """从 media_info 提取有序的 mp4 直链候选。

    优先取新版多码率数组 playback_list 里的 mp4 档（m3u8 是 HLS 切片，
    QQ 视频消息吃不了），hd 按清晰度降序、sd 升序；其后是旧版字段的
    降级链。返回的每个 URL 都可直接下载，签名会过期，拿到就要马上用。
    """
    cands: list[str] = []
    seen: set[str] = set()
    playlist: list[tuple[int, int, str]] = []
    pl = mi.get("playback_list")
    if isinstance(pl, list):
        for e in pl:
            if not isinstance(e, dict):
                continue
            info = e.get("play_info") or {}
            url = _abs_https(str(info.get("url") or ""))
            if not url or url in seen:
                continue
            mime = str(info.get("mime") or "")
            if url.lower().split("?")[0].endswith(".m3u8") or "mpegurl" in mime:
                continue
            try:
                width = int(info.get("width") or 0)
            except (TypeError, ValueError):
                width = 0
            try:
                size = int(info.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            seen.add(url)
            playlist.append((width, size, url))
    playlist.sort(key=lambda t: (t[0], t[1]), reverse=prefer_hd)
    cands.extend(u for _, _, u in playlist)
    for key in (
        "replay_hd",
        "stream_url_hd",
        "stream_url",
        "mp4_hd_url",
        "mp4_720p_mp4",
        "mp4_sd_url",
        "h5_url",
        "video_clear",
    ):
        url = _abs_https(str(mi.get(key) or ""))
        if url and url not in seen and not url.lower().split("?")[0].endswith(".m3u8"):
            seen.add(url)
            cands.append(url)
    return cands


def _mix_media_nodes(mb: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析图文视频混排结构 mix_media_info。

    返回 (图片节点列表——与 _pic_nodes 的条目同构，可直接交给 _extract_pics 的
    逐项转换逻辑, 视频的 media_info 列表)。各接口变体里媒体对象可能挂在
    item.data / item.data.pic / item.page_info 下，逐层探测。
    """
    pic_nodes: list[dict[str, Any]] = []
    medias: list[dict[str, Any]] = []
    mix = mb.get("mix_media_info")
    items = mix.get("items") if isinstance(mix, dict) else None
    if not isinstance(items, list):
        return pic_nodes, medias
    for it in items:
        if not isinstance(it, dict):
            continue
        data = it.get("data")
        node = data if isinstance(data, dict) else it
        mtype = str(it.get("type") or "")
        if mtype == "video":
            mi = node.get("media_info")
            if not isinstance(mi, dict):
                pi = node.get("page_info")
                if isinstance(pi, dict):
                    mi = pi.get("media_info")
            if isinstance(mi, dict):
                medias.append(mi)
        elif mtype == "pic":
            pic = node.get("pic")
            if isinstance(pic, dict):
                pic_nodes.append(pic)
            elif node.get("url") or node.get("pid"):
                pic_nodes.append(node)
    return pic_nodes, medias


def _extract_pics(
    mb: dict[str, Any], rt_mb: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """从 mblog 提取图片列表（转发微博时原微博的图也算），视频条目一律跳过。

    只有 kind=pic（普通图）与 kind=live（实况图，video_url 为视频段）会进入
    相册上传链路；纯视频条目与"带视频地址但非实况图"的条目（视频封面等）
    都不提取——群相册只收图片，检测到视频就不传。

    每项：{"pid","url"(改写后的 /large/ 原图),"alt_url"(接口原地址，原图 404 时兜底),
    "ext","kind","video_url"}。
    """
    imgs: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in (mb, rt_mb):
        if not node:
            continue
        for p in _pic_nodes(node):
            largest = p.get("largest") or {}
            large = p.get("large") or {}
            base = (
                largest.get("url")
                or large.get("url")
                or (p.get("original") or {}).get("url")
                or p.get("url")
                or ""
            )
            if not base:
                continue
            seg = str(base).split("?")[0].rstrip("/").split("/")
            pid = str(p.get("pid") or "")
            if not pid:
                # 接口缺 pid 时从 URL 提取：sinaimg 路径是 /<尺寸token>/<pid>.<ext>，
                # 文件名主干才是 pid（取 seg[-2] 会拿到 "large" 这类尺寸段，
                # 整批图共享同一个 pid，去重会把多张压成一张、本地文件互相覆盖）
                pid = seg[-1].rsplit(".", 1)[0] if seg and "." in seg[-1] else ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            ptype = str(p.get("type") or "")
            video = str(p.get("videoSrc") or p.get("video_src") or "")
            if ptype == "video" or (video and ptype != "livephoto"):
                # 群相册仅支持图片：纯视频条目跳过；带视频地址但不是实况图的
                # 条目（接口变体里的视频封面等）也按视频处理，一律不上传
                continue
            ext = (seg[-1].rsplit(".", 1)[-1] if "." in seg[-1] else "jpg").lower()
            kind = "live" if video else "pic"
            orig = to_original(str(base))
            imgs.append(
                {
                    "pid": pid,
                    "url": orig,
                    "alt_url": "" if orig == str(base) else str(base),
                    "ext": ext,
                    "kind": kind,
                    "video_url": video if kind == "live" else "",
                }
            )
    return imgs


def _pic_stem(pic: dict[str, str]) -> str:
    """本地文件名主干：必须用整串 pid（同一博主多张图的 pid 前缀可能完全相同）。"""
    pid = str(pic.get("pid") or "")
    if pid:
        return BAD_NAME_RE.sub("", pid)[:64] or "pic"
    return hashlib.sha1(str(pic.get("url") or "").encode("utf-8")).hexdigest()[:16]


def _mark(pic: dict[str, str]) -> str:
    """相册里的稳定标识：整串 pid（或 URL 摘要），用来判断"这张已经传过"。"""
    return _pic_stem(pic).lower()


def _parse_album_rules(raw_rules) -> dict[tuple[str, str], str]:
    """album_rules 配置列表 → {(uid, 群号): 相册名或ID}，条目格式 uid:群号:相册。"""
    rules: dict[tuple[str, str], str] = {}
    for item in raw_rules or []:
        parts = str(item).strip().split(":", 2)
        if len(parts) != 3:
            continue
        uid, gid, album = (p.strip() for p in parts)
        if uid.isdigit() and gid.isdigit() and album:
            rules[(uid, gid)] = album
    return rules


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
        # SSRF 校验用 DNS 缓存：host -> (getaddrinfo 结果, 过期 monotonic)
        self._dns_cache: dict[str, tuple[list[tuple[Any, ...]], float]] = {}
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
        # ---- 群相册自动上传 ----
        self._album_tasks: set[asyncio.Task] = set()
        # ---- 视频转发 ----
        self._video_tasks: set[asyncio.Task] = set()
        self._video_preparing: set[str] = set()  # 正在后台下载视频的 post_id
        # 上传并发闸 + 在途 base64 载荷字节预算（配置热改时重建）
        self._up_sem: asyncio.Semaphore | None = None
        self._up_conc = 0
        self._up_bytes = 0
        self._up_cond: asyncio.Condition | None = None
        self._dl_sem: asyncio.Semaphore | None = None
        self._dl_conc = 0
        self._cpu_sem = asyncio.Semaphore(2)  # ffmpeg 转码这类重 CPU 活的全局闸
        self._album_locks: dict[str, asyncio.Lock] = {}  # 同群批次串行化
        # (群, 相册标识) -> (时间戳, album_id, album_name)
        self._album_cache: dict[str, tuple[float, str, str]] = {}
        self._self_ids: dict[int, str] = {}  # id(bot) -> 登录账号 self_id 缓存
        self._ffmpeg = ""  # ffmpeg 可执行文件路径，initialize 时探测
        self._diag_path: Path | None = None  # 相册上传诊断文件（initialize 时解析）
        # ---- WebUI 控制面板（dashboard.py 注册路由，旧版 AstrBot 自动降级）----
        self._web_ready = False
        self._manual_check_task: asyncio.Task | None = None
        # 运行统计与最近动态（随 state.json 持久化，供面板展示）
        self.stats: dict[str, Any] = {
            "push_ok": 0,
            "push_fail": 0,
            "album_uploaded": 0,
            "album_skipped": 0,
            "album_failed": 0,
            "by_uid": {},  # uid -> {"name", "push_ok"}
            "daily": {},  # "YYYY-MM-DD" -> {"push_ok", "push_fail"}
        }
        self.activity: deque[dict[str, Any]] = deque(maxlen=ACTIVITY_MAX)

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._load_state()
        self._sync_accounts_from_config()
        # 实况图转 GIF 依赖 ffmpeg（缺失时回落封面静图，不影响其余功能）
        self._ffmpeg = shutil.which("ffmpeg") or ""
        if not self._ffmpeg:
            logger.info(f"{PLUGIN_NAME} 未找到 ffmpeg，实况图将只上传封面静图")
        # 诊断文件随启动记录一份流控配置快照，配置改了对不上号时以它为准
        self._diag_path = self._data_dir / DIAG_FILE
        await self._diag(
            f"启动 ffmpeg={'有' if self._ffmpeg else '无'} "
            f"album_download_concurrency={self._int_cfg('album_download_concurrency', 5)} "
            f"album_upload_concurrency={self._int_cfg('album_upload_concurrency', 3)} "
            f"album_upload_payload_mb={self._float_cfg('album_upload_payload_mb', 32.0):g} "
            f"album_live_gif={bool(self.config.get('album_live_gif', True))} "
            f"message_with_videos={self._videos_enabled()} "
            f"video_max_mb={self.config.get('video_max_mb')} "
            f"video_quality={self.config.get('video_quality')}"
        )
        # 清掉上次运行残留的下载临时文件（正常流程传完即删，这里只兜底）
        await asyncio.to_thread(self._wipe_album_tmp)
        await asyncio.to_thread(self._wipe_video_tmp)
        # DummyCookieJar：cookie 全部手工管理，避免与 jar 自动附带冲突。
        # 连接器 DNS 缓存放宽到 5 分钟（默认 10 秒）：微博系 host 极少变更，
        # 与校验侧的 _dns_cache 一起把每批图片几十次解析压到个位数
        self._http = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(ttl_dns_cache=300),
        )
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            if proxy.startswith(("http://", "https://")):
                self._proxy = proxy
                logger.info(f"{PLUGIN_NAME} 使用代理出网: {_redact_proxy(proxy)}")
            else:
                logger.warning(
                    f"{PLUGIN_NAME} proxy 配置无效（需 http:// 或 https:// 开头），"
                    f"已忽略: {_redact_proxy(proxy)}"
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
        self._web_ready = register_dashboard(
            self, PLUGIN_NAME, PLUGIN_VERSION, _extract_uid, _redact_proxy
        )
        if self._web_ready:
            logger.info(
                f"{PLUGIN_NAME} WebUI 控制面板已就绪"
                "（AstrBot 管理面板 - 插件 - 本插件详情页打开）"
            )
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
        # 面板触发的手动检查也要撤掉，避免 terminate 后还在写状态
        if self._manual_check_task:
            self._manual_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._manual_check_task
            self._manual_check_task = None
        # 相册上传是独立任务，要在关掉 HTTP 会话之前撤掉（在途下载引用着 session）
        for task in list(self._album_tasks):
            task.cancel()
        if self._album_tasks:
            await asyncio.gather(*self._album_tasks, return_exceptions=True)
            self._album_tasks.clear()
        # 视频下载任务同样在关 HTTP 会话之前撤掉
        for task in list(self._video_tasks):
            task.cancel()
        if self._video_tasks:
            await asyncio.gather(*self._video_tasks, return_exceptions=True)
            self._video_tasks.clear()
        self._save_state()
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None
        await asyncio.to_thread(self._wipe_album_tmp)
        await asyncio.to_thread(self._wipe_video_tmp)
        logger.info(f"{PLUGIN_NAME} 已停止")

    # ---------------- 配置与状态 ----------------

    def _sessions(self) -> list[str]:
        return [str(s) for s in (self.config.get("push_sessions") or [])]

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.config.get(key) or default))
        except (TypeError, ValueError):
            return default

    def _float_cfg(self, key: str, default: float) -> float:
        try:
            v = float(self.config.get(key) or default)
        except (TypeError, ValueError):
            return default
        return v if v >= 0 else default

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
                # v1.4.0 起待推送条目携带图片信息，旧状态文件没有该字段
                if not isinstance(item.get("pics"), list):
                    item["pics"] = []
                # v1.6.0 起携带视频信息；旧条目里的 video_file 路径若已随
                # video_tmp 清空失效，_video_ready 会自动重置为待下载
                if not isinstance(item.get("videos"), list):
                    item["videos"] = []
                if not isinstance(item.get("video_files"), list):
                    item["video_files"] = []
                item.setdefault("video_note", "")
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
        # v1.5.0 起随状态文件持久化运行统计与最近动态（旧文件缺省时用默认值）
        stats = state.get("stats")
        if isinstance(stats, dict):
            merged = dict(self.stats)
            for key in (
                "push_ok",
                "push_fail",
                "album_uploaded",
                "album_skipped",
                "album_failed",
            ):
                try:
                    merged[key] = max(0, int(stats.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            if isinstance(stats.get("by_uid"), dict):
                merged["by_uid"] = {
                    str(u): d
                    for u, d in stats["by_uid"].items()
                    if str(u).isdigit() and isinstance(d, dict)
                }
            if isinstance(stats.get("daily"), dict):
                merged["daily"] = {
                    str(k): v
                    for k, v in stats["daily"].items()
                    if isinstance(v, dict)
                }
            self.stats = merged
        acts = state.get("activity")
        if isinstance(acts, list):
            self.activity.clear()
            for act in acts[-ACTIVITY_MAX:]:
                if not isinstance(act, dict):
                    continue
                self.activity.append(
                    {
                        "ts": float(act.get("ts") or 0),
                        "kind": str(act.get("kind") or "info"),
                        "uid": str(act.get("uid") or ""),
                        "name": str(act.get("name") or ""),
                        "text": str(act.get("text") or ""),
                        "detail": str(act.get("detail") or ""),
                    }
                )
        self._prune_stats()

    def _save_state(self):
        if self._data_dir is None:
            return
        self._prune_stats()
        state = {
            "version": 2,
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
            "stats": self.stats,
            "activity": list(self.activity),
        }
        try:
            # 先写临时文件再原子替换：进程恰好死在写入中途不会留下截断的
            # state.json（截断会让下次启动丢掉游客身份与整个待推送队列）
            tmp = self._data_dir / "state.json.tmp"
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._state_file())
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 保存 state.json 失败: {e}")
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # ---------------- 运行统计与最近动态（WebUI 面板数据源） ----------------

    def _account_name(self, uid: str) -> str:
        return str(self.accounts.get(uid, {}).get("name") or uid)

    def _record_event(
        self,
        kind: str,
        uid: str = "",
        name: str = "",
        text: str = "",
        detail: str = "",
    ):
        """记一条最近动态（面板展示用）；text/detail 截断防 state.json 膨胀。"""
        self.activity.append(
            {
                "ts": time.time(),
                "kind": kind,
                "uid": str(uid or ""),
                "name": str(name or ""),
                "text": str(text or "")[:ACTIVITY_TEXT_MAX],
                "detail": str(detail or "")[:ACTIVITY_TEXT_MAX],
            }
        )

    def _record_push(self, uid: str, ok: bool, text: str, detail: str = ""):
        """推送成功/放弃的统一记账：动态 + 累计 + 按天 + 按博主。"""
        key = "push_ok" if ok else "push_fail"
        self.stats[key] = int(self.stats.get(key, 0)) + 1
        day = datetime.now().strftime("%Y-%m-%d")
        bucket = self.stats.setdefault("daily", {}).setdefault(
            day, {"push_ok": 0, "push_fail": 0}
        )
        bucket[key] = int(bucket.get(key, 0)) + 1
        name = self._account_name(uid)
        if uid:
            entry = self.stats.setdefault("by_uid", {}).setdefault(
                str(uid), {"name": "", "push_ok": 0}
            )
            entry["name"] = name
            if ok:
                entry["push_ok"] = int(entry.get("push_ok", 0)) + 1
        self._record_event("push" if ok else "push_fail", uid=uid, name=name, text=text, detail=detail)

    def _record_album(self, uploaded: int, skipped: int, failed: int, detail: str):
        """相册上传结果记账：张数进累计，批次进最近动态。"""
        self.stats["album_uploaded"] = int(self.stats.get("album_uploaded", 0)) + uploaded
        self.stats["album_skipped"] = int(self.stats.get("album_skipped", 0)) + skipped
        self.stats["album_failed"] = int(self.stats.get("album_failed", 0)) + failed
        self._record_event("album_fail" if failed else "album", detail=detail)

    def _prune_stats(self):
        """统计桶只留最近 STATS_DAILY_KEEP 天；博主计数只在长出头时裁掉已取消监控的。"""
        cutoff = (datetime.now() - timedelta(days=STATS_DAILY_KEEP)).strftime("%Y-%m-%d")
        daily = self.stats.get("daily") or {}
        self.stats["daily"] = {k: v for k, v in daily.items() if str(k) >= cutoff}
        by_uid = self.stats.get("by_uid") or {}
        if len(by_uid) > 64:
            self.stats["by_uid"] = {
                u: d for u, d in by_uid.items() if u in self.accounts
            }

    # ---------------- 网络请求 ----------------

    async def _resolve_host(self, host: str) -> list[tuple[Any, ...]]:
        """带 TTL 缓存的 getaddrinfo（仅 TCP 记录），供出网前的 SSRF 校验使用。

        白名单 host 只有寥寥几个且极少变更；aiohttp 连接时还会自己再解析一次，
        那份由连接器的 ttl_dns_cache 管，这里只兜校验自己的。解析失败不记负
        缓存，下一次请求照常重试真解析。
        """
        now = time.monotonic()
        hit = self._dns_cache.get(host)
        if hit:
            infos, expires = hit
            if now < expires:
                return infos
            self._dns_cache.pop(host, None)
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
        self._dns_cache[host] = (infos, now + DNS_CACHE_TTL)
        return infos

    @staticmethod
    def _ensure_public_addrs(infos: list[tuple[Any, ...]], label: str) -> None:
        """解析结果（新解析或缓存复用）里出现内网/保留地址就拦截。"""
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
                raise WeiboFetchError(f"{label}解析到受限地址，已拦截: {ip}")

    async def _validate_url(self, url: str):
        """出网前校验：仅 http/https、host 白名单、DNS 不解析到内网/保留地址。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WeiboFetchError(f"拒绝非 http(s) 协议: {parsed.scheme}")
        host = parsed.hostname or ""
        if host not in WEIBO_ALLOWED_HOSTS:
            raise WeiboFetchError(f"目标 host 不在白名单内: {host}")
        try:
            infos = await self._resolve_host(host)
        except OSError as e:
            raise WeiboFetchError(f"域名解析失败: {host} ({e})") from e
        self._ensure_public_addrs(infos, f"域名 {host} ")

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

    async def _get_text(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
        collect: dict[str, str] | None = None,
    ) -> str:
        await self._validate_url(url)
        await self._pace()
        assert self._http is not None
        timeout = aiohttp.ClientTimeout(total=15)
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with self._http.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout,
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

    async def _api_get(
        self, url: str, params: dict[str, Any], referer: str | None = None
    ) -> dict[str, Any]:
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

    async def _api_get_once(
        self, url: str, params: dict[str, Any], referer: str | None
    ) -> dict[str, Any]:
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
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = await self._get_text(url, params, headers)
        if body.lstrip().startswith("<"):
            # 风控拦截页与错误页照样回 200：JSON 接口拿到 HTML 说明这个 IP
            # 已经被墙了，按风控处理进入全局冷却，而不是按账号级失败继续撞墙
            # （与媒体下载对 HTML 200 的判定口径一致）
            raise WeiboRiskError("JSON 接口返回了 HTML 页面（疑似被风控拦截）")
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
        self._record_event(
            "risk",
            detail=(
                f"触发微博风控，全局冷却 {cooldown // 60} 分钟"
                f"（连续第 {level + 1} 次触发）"
            ),
        )
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
            await self._get_text(
                WEIBO_HOME_URL, {}, self._base_headers(), collect=cookies
            )
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
                    f"{datetime.fromtimestamp(self._renew_fail_until, tz=_TZ_CN).strftime('%H:%M:%S')} 后自动重试"
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
            except Exception:
                self._renew_fail_count += 1
                backoff = min(
                    3600.0, RENEW_FAIL_BACKOFF * (2 ** (self._renew_fail_count - 1))
                )
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
            raise WeiboFetchError(
                f"genvisitor 失败: {gen.get('retcode')} {gen.get('msg')}"
            )
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
        await self._get_text(
            WEIBO_INCARNATE_URL, inc_params, visitor_headers, collect=cookies
        )

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

    async def _apply_full_text(self, post: dict[str, Any]):
        """show_full_weibo_text 开启时拉取超长微博全文，替换摘要。

        只对确定要推送的新微博调用：旧微博反正进不了推送，拉全文是白花请求。
        覆盖外层微博与转发原微博两处，每条最多 2 次请求；任一获取失败安全回退
        摘要。已实测 /statuses/extend 游客身份可用（ok=1，data.longTextContent
        为含 HTML 的全文）。
        """
        if post["id"] and post.get("is_long"):
            full = await self._fetch_long_text(post["id"])
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

    # ---------------- 视频提取（statuses/show 详情接口） ----------------

    @staticmethod
    def _video_nodes(
        pi: Any, prefer_hd: bool, seen_urls: set[str]
    ) -> list[dict[str, Any]]:
        """从 page_info 提取一个视频的直链候选列表；直播中或无 media_info 时返回空。"""
        out: list[dict[str, Any]] = []
        if not isinstance(pi, dict) or str(pi.get("type") or "") != "video":
            return out
        if int(pi.get("live_status") or 0) == 1:
            logger.info("检测到直播中的视频，暂不支持转发（可点原帖链接观看）")
            return out
        mi = pi.get("media_info")
        if not isinstance(mi, dict):
            return out
        urls = [u for u in _video_candidates(mi, prefer_hd) if u not in seen_urls]
        seen_urls.update(urls)
        if urls:
            out.append({"urls": urls})
        return out

    async def _apply_video(self, post: dict[str, Any]) -> None:
        """视频帖拉取 statuses/show 详情，提取直链候选到 post["videos"]。

        只对确定要推送的微博调用（时间线接口的 page_info 里常缺 media_info，
        详情接口才有完整视频数据）。覆盖纯视频帖、图文混排（mix_media_info）
        与转发原微博的视频；混排里的图片一并并入 post["pics"]。任何失败都
        安全降级：videos 保持为空，推送时自动回落"请点原帖链接"提示。
        """
        if not post.get("is_video") or not self._videos_enabled():
            return
        pid = str(post["id"])
        prefer_hd = str(self.config.get("video_quality") or "hd") != "sd"
        try:
            data = await self._api_get(
                WEIBO_SHOW_URL,
                {"id": pid},
                referer=f"https://m.weibo.cn/detail/{pid}",
            )
        except asyncio.CancelledError:
            raise
        except WeiboFetchError as e:
            logger.info(f"获取微博 {pid} 视频详情失败，推送时将提示打开原帖: {e}")
            return
        detail = data.get("data")
        if not isinstance(detail, dict):
            return
        videos: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        videos.extend(self._video_nodes(detail.get("page_info"), prefer_hd, seen_urls))
        mix_pics, mix_medias = _mix_media_nodes(detail)
        for mi in mix_medias:
            urls = [u for u in _video_candidates(mi, prefer_hd) if u not in seen_urls]
            seen_urls.update(urls)
            if urls:
                videos.append({"urls": urls})
        rt = detail.get("retweeted_status")
        if isinstance(rt, dict) and not videos:
            # 外层没有视频才看转发原微博（转发时原视频挂在 rt 的结构里）
            videos.extend(self._video_nodes(rt.get("page_info"), prefer_hd, seen_urls))
            _, rt_medias = _mix_media_nodes(rt)
            for mi in rt_medias:
                urls = [
                    u for u in _video_candidates(mi, prefer_hd) if u not in seen_urls
                ]
                seen_urls.update(urls)
                if urls:
                    videos.append({"urls": urls})
        cap = max(1, self._int_cfg("max_videos_per_post", 2))
        post["videos"] = videos[:cap]
        if mix_pics:
            # 混排帖的图可能不在 mblog.pics 里（只出现在 mix_media_info），并入图片列表
            merged = list(post.get("pics") or [])
            have = {str(p.get("pid") or "") for p in merged}
            for p in _extract_pics({"pics": mix_pics}):
                if str(p.get("pid") or "") not in have:
                    merged.append(p)
            post["pics"] = merged

    async def _fetch_timeline_page(self, uid: str, page: int) -> list[dict[str, Any]]:
        """拉取用户时间线的指定页，返回解析后的帖子列表（新→旧）。"""
        data = await self._api_get(
            WEIBO_INDEX_URL,
            {
                "type": "uid",
                "value": uid,
                "containerid": f"107603{uid}",
                "page": page,
                "count": 20,
            },
            referer=f"https://m.weibo.cn/u/{uid}",
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            return []
        cards = payload.get("cards") or []
        posts: list[dict[str, Any]] = []
        for card in cards:
            # 接口对已删除/隐藏的微博会在 cards 里留 null 槽位，条目不保证是对象
            if not isinstance(card, dict):
                continue
            if card.get("card_type") != 9:
                continue
            mb = card.get("mblog")
            if not isinstance(mb, dict):
                continue
            if self._is_pinned(card, mb):
                continue
            post = self._parse_post(mb, uid)
            if post:
                posts.append(post)
        return posts

    async def _fetch_timeline(
        self, uid: str, known_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """拉取用户最新微博，返回解析后的帖子列表（新→旧，跨页按 id 去重）。

        默认只拉第一页；整页都是未见过的微博说明两次检查之间可能积压了超过
        一页（轮询间隔较长或博主发博密集），向后翻页补漏，最多共
        TIMELINE_MAX_PAGES 页——否则更早的新微博会滑出第一页，永远漏掉。
        known_ids 为 None（建基线轮）时不翻页，历史微博反正不推送。
        """
        posts: list[dict[str, Any]] = []
        got: set[str] = set()
        for page in range(1, TIMELINE_MAX_PAGES + 1):
            batch = await self._fetch_timeline_page(uid, page)
            for post in batch:
                if post["id"] not in got:
                    got.add(post["id"])
                    posts.append(post)
            # 本页没有帖子，或页面里出现了已见过的微博（新旧边界已落在本页内）：
            # 更早的页全是旧微博，停止翻页
            if not batch or known_ids is None or any(
                p["id"] in known_ids for p in batch
            ):
                break
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

    def _parse_post(
        self, mb: dict[str, Any], fallback_uid: str
    ) -> dict[str, Any] | None:
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
        rt_mb: dict[str, Any] | None = None
        if is_retweet:
            rt_mb = mb.get("retweeted_status") or {}
            rt_user = rt_mb.get("user") or {}
            orig_name = str(rt_user.get("screen_name") or "")
            orig_text = _clean_text(str(rt_mb.get("text") or ""), limit)
            orig_id = str(rt_mb.get("id") or rt_mb.get("mid") or "")
            orig_is_long = bool(rt_mb.get("isLongText"))
        # 视频帖标志（实测 m.weibo.cn：纯视频微博 page_info.type == "video" 且无
        # pics 数组）。只用作相册上传跳过时的日志说明，是否上传始终以提取到的
        # 图片列表为准，避免未来出现"图文视频混排"时误跳过混排里的真图。
        pi = mb.get("page_info") if isinstance(mb.get("page_info"), dict) else {}
        rt_pi = rt_mb.get("page_info") if isinstance(rt_mb, dict) else {}
        is_video = (
            str(pi.get("type") or "") == "video"
            or str(rt_pi.get("type") or "") == "video"
        )

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
            # 本条是否超长（全文按需拉取时用，见 _apply_full_text）
            "is_long": bool(mb.get("isLongText")),
            "pics": _extract_pics(mb, rt_mb),
            "is_video": is_video,
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
        whitelist = [
            str(k).strip()
            for k in (self.config.get("whitelist_keywords") or [])
            if str(k).strip()
        ]
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
            # 是否产生了需要落盘的变化（新微博/基线/昵称修正/待推送队列变化）：
            # 无变化的常规轮次不写 state.json
            state_dirty = False
            entries = list(self.accounts.items())
            for pos, (uid, info) in enumerate(entries):
                if time.time() < self._blocked_until:
                    logger.warning(f"{PLUGIN_NAME} 风控冷却生效，本轮剩余账号跳过")
                    break
                if time.time() < info.get("next_retry_ts", 0):
                    continue
                try:
                    got, dirty = await self._check_account(uid)
                    new_count += got
                    state_dirty = state_dirty or dirty
                    info["fail_count"] = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    info["fail_count"] = info.get("fail_count", 0) + 1
                    fail = info["fail_count"]
                    backoff = (
                        min(600.0, 60.0 * 2 ** (fail - 1)) if fail >= 2 else 60.0
                    )
                    info["next_retry_ts"] = time.time() + backoff
                    if fail == 1:
                        # 只记首次失败，断网/风控持续期间不逐轮刷屏
                        self._record_event(
                            "error",
                            uid=uid,
                            name=self._account_name(uid),
                            detail=f"检查失败: {e}",
                        )
                    logger.warning(
                        f"检查账号 {uid} 失败（连续第 {fail} 次，{backoff:.0f}s 后重试）: {e}"
                    )
                # 多账号之间留随机间隔错开请求；最后一个账号之后没必要再等
                if pos < len(entries) - 1:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
            flushed = await self._flush_pending()
            self.last_check_ts = time.time()
            if state_dirty or flushed:
                self._save_state()
            return new_count
        finally:
            self._checking = False

    async def _check_account(self, uid: str) -> tuple[int, bool]:
        """检查单个账号，返回 (新检测到的微博数, 是否产生需要落盘的状态变化)。"""
        info = self.accounts[uid]
        # 已见 ID 按加入顺序保存，超出上限只裁最早记录的：去重用 set，但
        # list(set) 是哈希序，直接裁剪会随机丢 ID——单账号记录涨满 500 条后，
        # 每轮都有概率把刚推送过的新微博 ID 裁掉，下一轮再推一遍造成重复
        seen_list = [str(i) for i in (info.get("seen_ids") or [])][-SEEN_IDS_LIMIT:]
        seen: set[str] = set(seen_list)
        posts = await self._fetch_timeline(
            uid, seen if info.get("baseline_done") else None
        )
        dirty = False
        # 用接口数据修正昵称（添加时可能没拉到）
        if posts and info.get("name") == uid:
            info["name"] = posts[0]["author"]
            dirty = True
        if not info.get("baseline_done"):
            # 首次观察：只记录基线，不推送历史微博
            for post in posts:
                if post["id"] not in seen:
                    seen.add(post["id"])
                    seen_list.append(post["id"])
            info["seen_ids"] = seen_list[-SEEN_IDS_LIMIT:]
            info["baseline_done"] = True
            logger.info(f"账号 {uid} 基线已建立，共 {len(posts)} 条历史微博")
            return 0, True
        new_posts = [p for p in posts if p["id"] not in seen]
        if not new_posts:
            return 0, dirty
        include_rt = bool(self.config.get("include_retweets", True))
        show_full = bool(self.config.get("show_full_weibo_text", False))
        # 图片数上限：消息带图与相册上传共用（0/异常值回退默认）
        max_imgs = (
            self._int_cfg("album_max_images", DEFAULT_MAX_IMAGES) or DEFAULT_MAX_IMAGES
        )
        for post in reversed(new_posts):  # 旧→新依次入队
            seen.add(post["id"])
            seen_list.append(post["id"])
            if self._is_expired(post.get("created_ts", 0.0)):
                continue
            if post["is_retweet"] and not include_rt:
                continue
            if show_full:
                # 全文只补给确定要推的帖子：在时效/转发过滤之后、关键词过滤
                # 之前，保证关键词匹配看到的仍是全文
                await self._apply_full_text(post)
            hit = self._keyword_hit(post)
            if hit:
                logger.info(f"微博 {post['id']} {hit}，已跳过推送")
                continue
            if len(self.pending) >= PENDING_HARD_LIMIT:
                logger.warning("待推送队列已满，丢弃更早的新微博")
                continue
            # 视频详情按需拉取（每帖最多 1 次额外请求）：放在过滤之后，
            # 被过滤掉的帖子不花这个请求
            await self._apply_video(post)
            videos_enabled = self._videos_enabled()
            extracted = post.get("videos") or []
            self.pending.append(
                {
                    "post_id": post["id"],
                    "uid": post["uid"],
                    "text": self._build_message(post),
                    "created_ts": post.get("created_ts", 0.0),
                    "retries": 0,
                    "pics": (post.get("pics") or [])[:max_imgs],
                    "is_video": bool(post.get("is_video")),
                    "videos": extracted,
                    "video_files": [],
                    # 检测到视频但没提取到直链（直播/详情缺失）：直接降级提示，
                    # 否则 _video_ready 会一直等一个永远不会完成的下载
                    "video_note": (
                        VIDEO_FALLBACK_NOTE
                        if videos_enabled and post.get("is_video") and not extracted
                        else ""
                    ),
                }
            )
        info["seen_ids"] = seen_list[-SEEN_IDS_LIMIT:]
        return len(new_posts), True

    async def _build_chain(
        self, item: dict[str, Any], *, with_video: bool = True
    ) -> MessageChain:
        """构建推送消息链：文本 + 可选图片段 + 可选视频段（message_with_* 开启时）。

        with_video=False 供视频发送反复失败后的降级重发使用：只发图文并附
        说明，保证微博正文至少能到群里。
        """
        chain = MessageChain().message(item["text"])
        if bool(self.config.get("message_with_images", True)):
            for pic in item.get("pics") or []:
                url = str(pic.get("url") or "")
                if url:
                    # 轻量档：群里看够了，原图交给相册那条路
                    chain.chain.append(Image.fromURL(to_light(url)))
        if self._videos_enabled():
            note = str(item.get("video_note") or "")
            if with_video:
                for path in item.get("video_files") or []:
                    p = Path(str(path))
                    if p.is_file():
                        chain.chain.append(await self._video_segment(p))
            elif item.get("video_files"):
                note = note or VIDEO_FALLBACK_NOTE
            if note:
                chain.message("\n" + note)
        return chain

    async def _send_with_timeout(
        self, session: str, chain: MessageChain, *, timeout_seconds: int | None = None
    ) -> bool:
        """带超时的主动消息发送，防止单个适配器卡死拖住整个轮询任务。"""
        send_timeout = (
            self._int_cfg("message_send_timeout", 60)
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            if send_timeout > 0:
                ok = await asyncio.wait_for(
                    self.context.send_message(session, chain),
                    timeout=send_timeout,
                )
            else:
                ok = await self.context.send_message(session, chain)
            if ok is False:
                raise RuntimeError("AstrBot 未找到匹配的消息平台")
            return True
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"发送超过 {send_timeout} 秒，平台是否已接收未知") from e

    def _pending_discard(self, item: dict[str, Any]):
        """按对象身份从待推送队列移除一条（内容相同的两条互不误伤）。

        已下载的视频临时文件一并删除：条目离队的三种出口（已推送/放弃/过期）
        都不再需要它。
        """
        for i, it in enumerate(self.pending):
            if it is item:
                del self.pending[i]
                for f in item.get("video_files") or []:
                    Path(str(f)).unlink(missing_ok=True)
                return

    async def _flush_pending(self) -> bool:
        """把待推送队列发送到所有绑定的会话，返回本轮是否有需落盘的状态变化。

        已发送/放弃/过期的条目在处理到它时立即从内存队列移除：插件重载
        （WebUI 保存配置即触发）或进程崩溃打断批量推送时，已发出的微博不会
        因为还留在 state.json 的 pending 里而在重启后重发一遍。落盘按
        STATE_SAVE_DEBOUNCE 合并：state.json 是全量序列化，积压批量推送时
        逐条落盘是纯写放大——首批照常立即落盘，之后隔一段合并一次，崩溃
        安全窗口最多放宽一个合并间隔。
        """
        max_retries = self._int_cfg("max_pending_retries", 20)
        delay = self._int_cfg("push_delay_seconds", 2)
        sessions = self._sessions()
        items = list(self.pending)  # 快照：循环中会从 self.pending 移除已完成条目
        total = len(items)
        changed = False
        last_save = float("-inf")
        for idx, item in enumerate(items):
            if self._is_expired(item.get("created_ts", 0.0)):
                logger.info(f"待推送微博 {item['post_id']} 已超过时效上限，自动清除")
                self._pending_discard(item)
                changed = True
                continue
            if not sessions:
                # 未绑定推送目标：保留消息但不消耗重试次数
                continue
            if item.get("retries", 0) >= max_retries:
                self._record_push(
                    str(item.get("uid") or ""),
                    False,
                    str(item.get("text") or ""),
                    f"重试 {item.get('retries', 0)} 轮仍失败，放弃推送",
                )
                logger.warning(f"推送重试超限，放弃微博 {item['post_id']}")
                await self._last_chance_flush(item, sessions)
                self._pending_discard(item)
                changed = True
                continue
            if not self._video_ready(item):
                # 视频还在后台下载：本轮跳过，下轮再发。不消耗重试次数——
                # 下载不是失败，烧完重试配额会把已经下好的视频一起弄丢
                self._ensure_video_prepare(item)
                continue
            sent_sessions: list[str] = []
            chain = await self._build_chain(item)
            send_timeout = self._int_cfg("message_send_timeout", 60)
            if send_timeout and item.get("video_files"):
                send_timeout = max(send_timeout, VIDEO_SEND_TIMEOUT_FLOOR)
            for session in sessions:
                try:
                    if await self._send_with_timeout(
                        session, chain, timeout_seconds=send_timeout
                    ):
                        sent_sessions.append(session)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"推送到 {session} 失败: {e!r}")
            if sent_sessions:
                self._pending_discard(item)
                logger.info(f"已推送微博 {item['post_id']}")
                self._record_push(
                    str(item.get("uid") or ""),
                    True,
                    str(item.get("text") or ""),
                    f"推送到 {len(sent_sessions)} 个会话",
                )
                self._dispatch_album_uploads(item, sent_sessions)
                changed = True
                now_mono = time.monotonic()
                if now_mono - last_save >= STATE_SAVE_DEBOUNCE:
                    self._save_state()
                    last_save = now_mono
            else:
                item["retries"] = item.get("retries", 0) + 1
                changed = True
            # 多条之间留间隔防刷屏；最后一条之后无需再等
            if delay and idx < total - 1:
                await asyncio.sleep(delay)
        if not sessions and len(self.pending) > PENDING_HARD_LIMIT:
            self.pending = self.pending[-PENDING_HARD_LIMIT:]
            changed = True
        return changed

    async def _last_chance_flush(
        self, item: dict[str, Any], sessions: list[str]
    ) -> None:
        """重试超限放弃前的最后一搏：带视频的条目去掉视频段（正文+图+降级提示）
        再各发一次。带毒的视频段（超 QQ 硬限、协议端不支持等）会把微博正文
        一起拖死——整条消息在每一轮都失败，一条都出不去。
        """
        if not sessions or not self._videos_enabled() or not item.get("video_files"):
            return
        try:
            chain = await self._build_chain(item, with_video=False)
        except Exception as e:
            logger.warning(f"微博 {item['post_id']} 降级消息构建失败: {e!r}")
            return
        for session in sessions:
            try:
                if await self._send_with_timeout(session, chain):
                    logger.info(
                        f"微博 {item['post_id']} 视频段多次发送失败，"
                        f"已去视频降级重发到 {session}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"微博 {item['post_id']} 降级重发到 {session} 仍失败: {e!r}"
                )

    # ---------------- 视频转发 ----------------

    def _videos_enabled(self) -> bool:
        return bool(self.config.get("message_with_videos", True))

    def _video_tmp_root(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / VIDEO_TMP_DIR

    def _wipe_video_tmp(self):
        """清掉上次运行残留的视频临时文件（正常流程发完即删，这里只兜底）。"""
        shutil.rmtree(self._video_tmp_root(), ignore_errors=True)

    @staticmethod
    def _callback_api_base() -> str:
        if astrbot_config is None:
            return ""
        try:
            return str(astrbot_config.get("callback_api_base") or "").strip()
        except Exception:
            return ""

    async def _video_segment(self, p: Path) -> Video:
        """构造视频段：默认 base64 内嵌，配置了 callback_api_base 才走本地路径。

        aiocqhttp 出站时 Image/Record 会被 AstrBot 自动转成 base64 内嵌，Video
        却把 file:// 路径原样透传——NapCat 与 AstrBot 分容器部署（互相看不到
        对方文件系统，Docker 分容器是常态）时，协议端打开这个路径必然 ENOENT，
        整条消息（含正文和图片）一起失败。base64 内嵌与图片同一条路，不依赖
        任何路径映射或额外配置；代价是发送瞬间内存放大 ~1.33 倍，体积已被
        video_max_mb 封顶。配了 callback_api_base 时 to_dict() 会把非 http 的
        file 值交给 register_file 注册成回调下载地址，那条路只认本地文件，
        base64 串会炸，所以保持 fromFileSystem 让 AstrBot 自己去注册。
        """
        if self._callback_api_base():
            return Video.fromFileSystem(path=str(p))
        try:
            raw_b64 = await asyncio.to_thread(
                lambda: base64.b64encode(p.read_bytes()).decode("ascii")
            )
        except OSError as e:
            # 读不动（文件刚被清掉 / 权限问题）：退回路径模式，不比旧行为差
            logger.warning(f"视频 {p.name} 读取失败，回退本地路径发送: {e!r}")
            return Video.fromFileSystem(path=str(p))
        return Video.fromBase64(raw_b64)

    def _video_ready(self, item: dict[str, Any]) -> bool:
        """条目的视频是否已就绪可发送：未开启 / 无视频 / 已下载 / 已降级 都算就绪。

        已下载的文件在磁盘上消失（重启清空 video_tmp）时清掉旧结论回到待下载，
        由后台任务重新下载——直链可能已过期，再失败就重新降级提示链接。
        """
        if not self._videos_enabled():
            return True
        files = [str(f) for f in item.get("video_files") or [] if f]
        if files:
            alive = [f for f in files if Path(f).is_file()]
            if len(alive) != len(files):
                item["video_files"] = alive
                files = alive
            if files:
                return True
            # 文件全部失效：清掉上一次的降级结论，按候选直链重新下载
            item["video_note"] = ""
        # 下载已有定论（全失败/部分失败都记了提示）就不再等后台任务
        if item.get("video_note"):
            return True
        # 没有本地文件也没有定论：还有候选直链就等下载，否则按无视频处理
        return not (item.get("videos") or [])

    def _ensure_video_prepare(self, item: dict[str, Any]):
        """为条目派发视频下载任务（同一微博同时只有一个，任务不阻塞轮询循环）。"""
        post_id = str(item.get("post_id") or "")
        if post_id in self._video_preparing:
            return
        self._video_preparing.add(post_id)
        task = asyncio.create_task(self._video_prepare(item))
        self._video_tasks.add(task)
        task.add_done_callback(self._video_tasks.discard)

    async def _video_prepare(self, item: dict[str, Any]) -> None:
        """后台下载条目视频到 video_tmp：成功填 video_files，失败填降级提示。

        与相册下载共用 _dl_gate 并发闸（微博媒体下载总并发不因视频新增一路）；
        下载前过磁盘保护线。下载结束回写 state.json，重启后不必重下；若条目
        已被丢弃（过期/放弃/重发），产物不留死角。
        """
        post_id = str(item.get("post_id") or "")
        entries = [v for v in (item.get("videos") or []) if isinstance(v, dict)]
        files: list[str] = []
        try:
            try:
                cap_mb = int(self.config.get("video_max_mb"))
            except (TypeError, ValueError):
                cap_mb = VIDEO_MAX_MB_DEFAULT
            max_bytes: int | float = (
                cap_mb * 1048576 if cap_mb > 0 else float("inf")
            )
            root = self._video_tmp_root()
            root.mkdir(parents=True, exist_ok=True)
            free_mb = (
                await asyncio.to_thread(lambda: shutil.disk_usage(root).free)
            ) // 1048576
            if free_mb < DISK_FLOOR_MB:
                raise WeiboFetchError(
                    f"临时目录所在磁盘仅剩 {free_mb}MB"
                    f"（低于 {DISK_FLOOR_MB}MB 保护线），已取消视频下载"
                )
            stem_base = BAD_NAME_RE.sub("", post_id)[:48] or "video"
            for idx, v in enumerate(entries):
                dest = root / f"{stem_base}_{idx}.mp4"
                got = False
                for url in [str(u) for u in (v.get("urls") or []) if u]:
                    try:
                        await self._validate_media_url(url)
                        await self._stream_to_file(
                            url,
                            dest,
                            max_bytes=max_bytes,
                            timeout_total=VIDEO_DL_TIMEOUT,
                        )
                        files.append(str(dest))
                        got = True
                        break
                    except (
                        WeiboFetchError,
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                        OSError,
                    ) as e:
                        # 中断的下载会留下半截残件：删掉再试下一候选
                        dest.unlink(missing_ok=True)
                        logger.info(
                            f"微博 {post_id} 视频 {idx} 直链下载失败，尝试下一候选: {e!r}"
                        )
                if not got:
                    dest.unlink(missing_ok=True)
        except asyncio.CancelledError:
            for f in files:
                Path(f).unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.warning(f"微博 {post_id} 视频下载异常: {e!r}")
        finally:
            self._video_preparing.discard(post_id)
            if all(queued is not item for queued in self.pending):
                # 条目已从队列消失（过期/放弃）：刚下载的文件一并清掉
                for f in files:
                    Path(f).unlink(missing_ok=True)
            else:
                item["video_files"] = files
                if entries and len(files) < len(entries):
                    item["video_note"] = (
                        VIDEO_FALLBACK_NOTE if not files else VIDEO_PARTIAL_NOTE
                    )
                self._save_state()

    # ---------------- 群相册自动上传 ----------------

    def _album_tmp_root(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / ALBUM_TMP_DIR

    def _wipe_album_tmp(self):
        """清掉上次运行残留的下载临时文件（正常流程传完即删，这里只兜底）。"""
        shutil.rmtree(self._album_tmp_root(), ignore_errors=True)

    def _diag_write(self, msg: str) -> None:
        """诊断行的同步落盘实现，由 _diag 丢线程池执行（open/write/fsync 都是阻塞 syscall）。"""
        if self._diag_path is None:
            return
        try:
            self._diag_path.parent.mkdir(parents=True, exist_ok=True)
            if (
                self._diag_path.exists()
                and self._diag_path.stat().st_size > DIAG_MAX_BYTES
            ):
                lines = self._diag_path.read_text(encoding="utf-8").splitlines(True)
                self._diag_path.write_text(
                    "".join(lines[len(lines) // 2 :]), encoding="utf-8"
                )
            with open(self._diag_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass  # 诊断本身不能变成新的故障点

    async def _diag(self, msg: str) -> None:
        """关键诊断行落到独立的追加式文件（写后立即 fsync 刷盘，超 512KB 留后半段）。

        整机被内存顶死之后，AstrBot 主日志往往来不及刷/没机会看；这份文件在
        重负载动作发生"之前"落盘，重启后读它就能还原死机前最后几批的体量与
        配置，不用复现（与 astrbot_plugin_weibo_album 同一套做法）。
        """
        await asyncio.to_thread(self._diag_write, msg)

    def _album_rules(self) -> dict[tuple[str, str], str]:
        return _parse_album_rules(self.config.get("album_rules"))

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str]:
        """拆 unified_msg_origin：platform_id:message_type:session_id。"""
        parts = (umo or "").split(":", 2)
        if len(parts) != 3:
            return "", "", ""
        return parts[0], parts[1], parts[2]

    def _dispatch_album_uploads(self, item: dict[str, Any], sent_sessions: list[str]):
        """转发成功后按规则派发相册上传任务：每个命中规则的群一个异步任务。

        触发语义与"转发后上传"一致：只有该群真的推送成功才上传。任务不 await，
        不阻塞轮询循环；登记进 _album_tasks 供 terminate 撤销。
        """
        if not bool(self.config.get("album_enabled", True)):
            return
        pics = item.get("pics") or []
        uid = str(item.get("uid") or "")
        if not uid:
            return
        if not pics:
            # 群相册只收图片与实况图：没有可传内容（纯视频/纯文字微博）就不上传
            if item.get("is_video"):
                logger.info(
                    f"微博 {item.get('post_id')} 检测到视频、无图片/实况图，"
                    "跳过群相册上传"
                )
            return
        rules = self._album_rules()
        if not rules:
            return
        hit_any = False
        for session in sent_sessions:
            platform, msg_type, sid = self._parse_umo(session)
            if msg_type != "GroupMessage" or not sid:
                continue
            want = rules.get((uid, sid))
            if not want:
                continue
            hit_any = True
            task = asyncio.create_task(
                self._album_upload_task(session, platform, sid, dict(item), want)
            )
            self._album_tasks.add(task)
            task.add_done_callback(self._album_tasks.discard)
        if hit_any:
            logger.info(f"微博 {item['post_id']} 命中相册绑定规则，开始自动上传图片")

    def _find_bot(self, platform_id: str):
        """按 umo 的 platform_id 找 aiocqhttp 适配器实例的 CQHttp bot。

        监控插件是轮询型，手里没有消息事件，只能从 platform_manager 拿适配器实例
        （aiocqhttp 适配器把 OneBot 客户端挂在 .bot 上）。匹配不到 id 时退而取
        第一个带 bot 的实例，兼容老版本 AstrBot 的元数据缺 id 的情况。
        """
        pm = getattr(self.context, "platform_manager", None)
        get_insts = getattr(pm, "get_insts", None)
        if not callable(get_insts):
            return None
        for inst in get_insts():
            try:
                meta = getattr(inst, "metadata", None)
                if meta is None and hasattr(inst, "meta"):
                    meta = inst.meta()
                inst_id = str(getattr(meta, "id", "") or "")
            except Exception:
                continue
            if inst_id and platform_id and inst_id != platform_id:
                continue
            bot = getattr(inst, "bot", None)
            if bot is not None:
                return bot
        return None

    async def _album_client(self, platform_id: str) -> NapCatAlbum:
        """构造走 AstrBot 已有 OneBot 连接的相册客户端（地址与 token 全归适配器管）。"""
        bot = self._find_bot(platform_id)
        if bot is None:
            raise NapCatError(
                f"未找到平台 {platform_id or '(未知)'} 的 aiocqhttp 连接，无法上传群相册"
            )
        self_id = self._self_ids.get(id(bot))
        if self_id is None:
            try:
                info = await bot.call_action("get_login_info")
                self_id = str((info or {}).get("user_id") or "")
            except Exception as e:
                # 拿不到 self_id 不影响上传（多号同连一个协议端时才需要）
                logger.info(
                    f"{PLUGIN_NAME} 获取机器人登录信息失败（不影响上传）: {e!r}"
                )
                self_id = ""
            self._self_ids[id(bot)] = self_id

        async def caller(action: str, params: dict):
            if self_id:
                params["self_id"] = self_id
            return await bot.call_action(action, **params)

        try:
            preferred = str(await self.get_kv_data("payload:same_host", "") or "")
        except Exception:
            preferred = ""
        return NapCatAlbum(
            caller,
            preferred=preferred,
            same_host=bool(self.config.get("album_same_host", False)),
        )

    @staticmethod
    def _ledger_key(gid: str, album_id: str) -> str:
        return f"sent:{gid}:{album_id}"

    async def _sent_marks(self, gid: str, album_id: str) -> set[str]:
        """本插件往这个相册传过哪些图（按 pid 记，KV 台账）。

        base64 载荷会被 NapCat 改名成 randomUUID，相册里的文件名不再含微博 pid，
        只靠回读文件名去重会失效，所以自己记一份。
        """
        try:
            raw = await self.get_kv_data(self._ledger_key(gid, album_id), {}) or {}
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 读取上传台账失败，本次跳过台账去重: {e!r}")
            return set()
        now = time.time()
        out: set[str] = set()
        for m, ts in dict(raw).items():
            try:
                if now - float(ts) < LEDGER_TTL:
                    out.add(m)
            except (TypeError, ValueError):
                continue
        return out

    async def _remember(self, gid: str, album_id: str, marks: list[str]) -> None:
        if not marks:
            return
        key = self._ledger_key(gid, album_id)
        try:
            raw = dict(await self.get_kv_data(key, {}) or {})
        except Exception:
            raw = {}
        now = time.time()
        sent: dict[str, float] = {}
        for m, ts in raw.items():
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if now - t < LEDGER_TTL:
                sent[m] = t
        sent.update(dict.fromkeys(marks, now))
        if len(sent) > LEDGER_MAX:
            sent = dict(sorted(sent.items(), key=lambda kv: kv[1])[-LEDGER_MAX:])
        try:
            await self.put_kv_data(key, sent)
        except Exception as e:
            # 台账写不进去只损失去重能力，不影响这次已经传上去的图
            logger.warning(f"{PLUGIN_NAME} 写入上传台账失败: {e!r}")

    async def _existing_names(self, nc: NapCatAlbum, gid: str, album_id: str) -> str:
        """相册里已有媒体的文件名合集；读不到就返回空串（去重只是优化，不能成为故障点）。"""
        try:
            media = await nc.list_media(gid, album_id)
        except NapCatError as e:
            logger.info(f"{PLUGIN_NAME} 读取相册已有媒体失败，本次跳过文件名去重: {e}")
            return ""
        return " ".join(pick(m, MEDIA_NAME_KEYS) for m in media).lower()

    async def _resolve_album_cached(
        self, nc: NapCatAlbum, gid: str, want: str
    ) -> tuple[str, str]:
        """(群, 相册名/ID) -> (album_id, album_name)，命中缓存就不再翻相册列表。"""
        key = f"{gid}:{want}"
        hit = self._album_cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ALBUM_RESOLVE_TTL:
            return hit[1], hit[2]
        album_id, album_name = await nc.resolve_album(gid, want)
        self._album_cache[key] = (now, album_id, album_name)
        return album_id, album_name

    def _group_lock(self, gid: str) -> asyncio.Lock:
        lock = self._album_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._album_locks[gid] = lock
        return lock

    def _up_gate(self) -> asyncio.Semaphore:
        """上传并发总闸（配置热改时重建，在途任务拿旧闸跑完本批）。"""
        conc = max(1, self._int_cfg("album_upload_concurrency", 3))
        if conc != self._up_conc or self._up_sem is None:
            self._up_sem = asyncio.Semaphore(conc)
            self._up_conc = conc
        if self._up_cond is None:
            # 字节预算 Condition 全程复用不重建：在途任务还挂着旧实例，
            # 重建会把 _up_bytes 计数清零，预算就失真了
            self._up_cond = asyncio.Condition()
        return self._up_sem

    def _dl_gate(self) -> asyncio.Semaphore:
        """下载并发总闸。"""
        conc = max(1, self._int_cfg("album_download_concurrency", 5))
        if conc != self._dl_conc or self._dl_sem is None:
            self._dl_sem = asyncio.Semaphore(conc)
            self._dl_conc = conc
        return self._dl_sem

    async def _validate_media_url(self, url: str):
        """媒体下载前校验：仅 http(s)、host 限微博系 CDN、DNS 不解析到内网/保留地址。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WeiboFetchError(f"拒绝非 http(s) 协议的媒体地址: {parsed.scheme}")
        host = parsed.hostname or ""
        if not any(
            host == s.lstrip(".") or host.endswith(s) for s in MEDIA_ALLOWED_SUFFIXES
        ):
            raise WeiboFetchError(f"媒体 host 不在白名单内: {host}")
        try:
            infos = await self._resolve_host(host)
        except OSError as e:
            raise WeiboFetchError(f"媒体域名解析失败: {host} ({e})") from e
        self._ensure_public_addrs(infos, f"媒体域名 {host} ")

    async def _stream_to_file(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int | float = IMG_MAX_BYTES,
        timeout_total: float = 60.0,
    ):
        """流式下载到 dest（64KB 分块边下边写），超限中途抛错由调用方清理残件。

        图片与视频共用：视频体积大、下载慢，上限与总超时由调用方给。
        """
        assert self._http is not None
        headers = {
            "User-Agent": self._ua or UA_POOL[0],
            # 微博媒体 CDN 校验 Referer（视频 CDN 无此头直接 403）
            "Referer": "https://weibo.com/",
        }
        timeout = aiohttp.ClientTimeout(total=timeout_total)
        got = 0
        async with self._http.get(
            url, headers=headers, timeout=timeout, proxy=self._proxy
        ) as resp:
            resp.raise_for_status()
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            if "text/html" in ctype:
                # 风控页与错误页照样回 200，体积也常常过 1KB：只按大小判成功的话，
                # HTML 会被存成 .jpg 一路传进相册，问题要到很远的地方才爆
                raise WeiboFetchError(f"拿到的是 HTML 页面（多半被风控拦了）: {url}")
            with dest.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(65536):
                    got += len(chunk)
                    if got > max_bytes:
                        limit_mb = (
                            int(max_bytes // 1048576)
                            if isinstance(max_bytes, int)
                            else max_bytes / 1048576
                        )
                        raise WeiboFetchError(f"单个媒体超过 {limit_mb:g}MB 上限")
                    fh.write(chunk)

    async def _download_pic(self, pic: dict[str, str], dest: Path) -> Path:
        """下载一张微博图：先取 /large/ 原图，失败退回接口给的原始地址。"""
        last = ""
        for url in (pic.get("url"), pic.get("alt_url")):
            if not url:
                continue
            part = dest.with_name(dest.name + ".part")
            try:
                await self._validate_media_url(url)
                await self._stream_to_file(url, part)
            except WeiboFetchError as e:
                last = str(e)
                part.unlink(missing_ok=True)
                continue
            size = part.stat().st_size if part.is_file() else 0
            if size > 1024:
                part.replace(dest)
                return dest
            last = f"HTTP 响应过小（{size}B）"
            part.unlink(missing_ok=True)
        raise WeiboFetchError(
            f"图片下载失败 {pic.get('pid') or pic.get('url')}: {last}"
        )

    async def _to_gif(self, mp4: Path, gif: Path) -> bool:
        """实况图视频段转 GIF（限帧率压体积），失败返回 False 让上层回落封面静图。"""
        # ffmpeg 的 lanczos 缩放是实打实的满核负载，全进程最多同时跑 2 个转码
        async with self._cpu_sem:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp4),
                "-vf",
                "fps=10,scale=480:-2:flags=lanczos",
                "-loop",
                "0",
                str(gif),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False
        return proc.returncode == 0 and gif.is_file() and gif.stat().st_size > 0

    async def _live_gif(self, pic: dict[str, str], folder: Path) -> Path | None:
        """下载实况图的视频段并转 GIF；任何一步失败返回 None，由上层回落封面。"""
        stem = _pic_stem(pic)
        mp4, gif = folder / f"{stem}.mp4", folder / f"{stem}.gif"
        try:
            await self._validate_media_url(pic.get("video_url") or "")
            await self._stream_to_file(str(pic.get("video_url") or ""), mp4)
        except WeiboFetchError:
            mp4.unlink(missing_ok=True)
            return None
        try:
            ok = await self._to_gif(mp4, gif)
        except (OSError, ValueError) as e:
            logger.info(f"{PLUGIN_NAME} 实况图转 GIF 失败，改用封面静图: {e!r}")
            ok = False
        mp4.unlink(missing_ok=True)
        if ok and gif.is_file() and gif.stat().st_size > GIF_MAX_BYTES:
            # 超大 GIF 走 base64 时在途内存会放大到百 MB 级，单张超载荷预算还会
            # 空闸直行——宁可回落封面静图也不让它把小服务器顶穿
            logger.info(
                f"{PLUGIN_NAME} 实况图 GIF 超 {GIF_MAX_BYTES // 1048576}MB，改用封面静图"
            )
            ok = False
        if not ok:
            gif.unlink(missing_ok=True)  # 超限/失败的产物不留进批次目录
            return None
        return gif

    async def _download_pics(
        self, pics: list[dict[str, str]], folder: Path
    ) -> list[tuple[dict[str, str], Path]]:
        """整批并发下载到批次目录（实况图优先转 GIF，失败回落封面静图）。"""
        free_mb = (
            await asyncio.to_thread(
                lambda: shutil.disk_usage(self._album_tmp_root()).free
            )
        ) // 1048576
        if free_mb < DISK_FLOOR_MB:
            raise WeiboFetchError(
                f"临时目录所在磁盘仅剩 {free_mb}MB（低于 {DISK_FLOOR_MB}MB 保护线），"
                "已取消本批下载"
            )
        sem = self._dl_gate()
        want_gif = bool(self.config.get("album_live_gif", True)) and bool(self._ffmpeg)

        async def one(pic: dict[str, str]) -> tuple[dict[str, str], Path] | None:
            async with sem:
                if pic.get("kind") == "live" and want_gif and pic.get("video_url"):
                    gif = await self._live_gif(pic, folder)
                    if gif is not None:
                        return pic, gif
                ext = (
                    re.sub(r"[^0-9a-z]", "", (pic.get("ext") or "jpg").lower())[:5]
                    or "jpg"
                )
                path = folder / f"{_pic_stem(pic)}.{ext}"
                try:
                    await self._download_pic(pic, path)
                except WeiboFetchError as e:
                    logger.info(f"相册图片下载失败，跳过: {e}")
                    return None
                return pic, path

        results = await asyncio.gather(*(one(p) for p in pics), return_exceptions=True)
        files: list[tuple[dict[str, str], Path]] = []
        for pic, r in zip(pics, results, strict=True):
            if isinstance(r, BaseException):
                # 子任务被撤（热重载/关停）时也按跳过收下，别让 CancelledError
                # 从 gather 里穿出去把父任务一起带走
                logger.warning(f"相册图片下载异常，跳过: {r!r}")
            elif r is not None:
                files.append(r)
        return files

    def _batch_dir(self, gid: str, post_id: str) -> Path:
        """批次下载目录：目录名带群号与微博 id，群与群、批与批互不覆盖。"""
        base = (
            f"{BAD_NAME_RE.sub('', gid)[:24] or 'group'}_"
            f"{BAD_NAME_RE.sub('', post_id)[:24] or 'post'}_"
            f"{time.strftime('%Y%m%d-%H%M%S')}"
        )
        folder = self._album_tmp_root() / base
        seq = 1
        while folder.exists():
            seq += 1
            folder = self._album_tmp_root() / f"{base}_{seq}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def _upload_files(
        self,
        nc: NapCatAlbum,
        gid: str,
        album_id: str,
        album_name: str,
        files: list[tuple[dict[str, str], Path]],
    ) -> tuple[int, list[str], list[str]]:
        """并发上传一批本地文件，返回 (成功数, 失败明细, 成功 marks)。"""
        sem = self._up_gate()
        cond = self._up_cond
        # NapCat 传相册是"每张内部串行发 16KB 分片"，单张本身就慢，并发数就是吞吐；
        # 间隔只用来错开起点防撞频控，压得太狠就是纯等
        interval = self._float_cfg("album_upload_interval", 0.5)
        # 在途 base64 载荷字节预算：按 1.5 倍原图体积收紧（载荷在插件与 NapCat
        # 两侧各驻留一份编码串）；单张超预算的图在空闸时照常放行，不会死锁
        budget_mb = self._float_cfg("album_upload_payload_mb", 32.0)
        payload_budget = (
            float("inf") if budget_mb <= 0 else max(0.5, budget_mb) * 1048576 / 1.5
        )
        modes: dict[str, int] = {}

        async def push(pic: dict[str, str], path: Path):
            async with sem:
                size = path.stat().st_size
                async with cond:
                    while self._up_bytes and self._up_bytes + size > payload_budget:
                        await cond.wait()
                    self._up_bytes += size
                try:
                    try:
                        mode = await nc.upload_file(gid, album_id, album_name, path)
                    except (NapCatError, OSError) as e:
                        logger.warning(f"{PLUGIN_NAME} 相册上传失败 {_mark(pic)}: {e}")
                        return None, f"{_mark(pic)}：{e}"
                    if interval:
                        # 传完占着并发槽歇 interval 再让位：只错开起点的话，
                        # 槽位一空就放行，配置里的频控间隔会名存实亡
                        await asyncio.sleep(interval)
                    return mode or "ok", None
                finally:
                    async with cond:
                        self._up_bytes -= size
                        cond.notify_all()

        results: list[Any] = []
        # 同机但还没学到可用载荷时，第一张先单独探路：整批一起拿本地路径去撞 NapCat，
        # 两边不同机就是每张一条 ENOENT，协议端控制台刷一屏，插件这边每张都白跑一趟
        # 载荷降级。学到过（preferred 非空，或 modes[0] 已是 base64）就别再探了。
        probe = (
            bool(nc.same_host)
            and nc.modes[0] == "path"
            and not nc.preferred
            and len(files) > 1
        )
        if probe:
            try:
                results.append(await push(*files[0]))
            except Exception as e:  # 探路这张出意外也别拖垮整批
                logger.warning(f"{PLUGIN_NAME} 相册上传探路异常: {e!r}")
                results.append((None, f"{_mark(files[0][0])}：{e!r}"))
        rest = files[1:] if probe else files
        results += await asyncio.gather(
            *(push(p, path) for p, path in rest), return_exceptions=True
        )
        ok, fails, marks = 0, [], []
        for (pic, _), r in zip(files, results, strict=True):
            if isinstance(r, BaseException):
                fails.append(f"{_mark(pic)}：{r!r}")
            elif r[0]:
                ok += 1
                modes[r[0]] = modes.get(r[0], 0) + 1
                marks.append(_mark(pic))
            else:
                fails.append(r[1] or "未知错误")
        if nc.same_host and modes:
            # 同机探测学到的可用载荷方式记下来，下一批复用，别每批都撞一遍 ENOENT
            learned = max(modes, key=lambda k: modes[k])
            with contextlib.suppress(Exception):
                await self.put_kv_data("payload:same_host", learned)
        return ok, fails, marks

    async def _album_upload_task(
        self, session: str, platform: str, gid: str, item: dict[str, Any], want: str
    ):
        """单条微博 × 单个群的相册上传任务（转发成功后异步执行）。"""
        post_id = str(item.get("post_id") or "")
        pics = [p for p in (item.get("pics") or []) if isinstance(p, dict)]
        try:
            async with self._group_lock(gid):
                await self._upload_batch(session, platform, gid, post_id, want, pics)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_album(
                0,
                0,
                0,
                f"微博 {post_id} -> 群 {gid} 上传任务异常: {e}",
            )
            await self._diag(f"微博 {post_id} -> 群 {gid} 上传任务异常: {e!r}")
            logger.warning(
                f"{PLUGIN_NAME} 微博 {post_id} 图片上传群 {gid} 相册失败: {e!r}"
            )

    async def _upload_batch(
        self,
        session: str,
        platform: str,
        gid: str,
        post_id: str,
        want: str,
        pics: list[dict[str, str]],
    ):
        """一批图片的完整上传流程：解析相册 -> 去重 -> 下载 -> 上传 -> 台账 -> 清理。"""
        nc = await self._album_client(platform)
        album_id, album_name = await self._resolve_album_cached(nc, gid, want)
        sent = await self._sent_marks(gid, album_id)
        # 相册文件名回读只在 same_host 下才做：base64 载荷会被 NapCat 改名成 randomUUID，
        # 相册文件名里不含微博 pid，比对必然不命中，而它每次要翻最多 8 页媒体列表
        # （一页一次协议端往返，串行）。自记台账才是这条链路可靠的去重依据。
        existing = await self._existing_names(nc, gid, album_id) if nc.same_host else ""

        def _dup(pic: dict[str, str]) -> bool:
            mark = _mark(pic)
            # 子串匹配是有意的：QQ 侧重名会把文件名改成 "<原名> (1)"，整词比对会漏
            return bool(mark) and (mark in sent or mark in existing)

        todo = [p for p in pics if not _dup(p)]
        dup = len(pics) - len(todo)
        if not todo:
            self._record_album(
                0,
                len(pics),
                0,
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」："
                f"{len(pics)} 张此前已传过，跳过",
            )
            logger.info(
                f"微博 {post_id} 的 {len(pics)} 张图此前已传过相册「{album_name}」"
                f"（群 {gid}），跳过"
            )
            return
        folder = self._batch_dir(gid, post_id)
        live_n = sum(1 for p in todo if p.get("kind") == "live")
        await self._diag(
            f"批次 {folder.name}：待下载 {len(todo)} 张（live {live_n} 张）"
            f"-> 群 {gid} 相册「{album_name}」"
        )
        try:
            dl_t0 = time.monotonic()
            files = await self._download_pics(todo, folder)
            dl_sec = time.monotonic() - dl_t0
            if not files:
                await self._diag(
                    f"批次 {folder.name}：{len(todo)} 张全部下载失败"
                )
                self._record_album(
                    0,
                    0,
                    len(todo),
                    f"微博 {post_id} -> 群 {gid} 相册「{album_name}」："
                    f"{len(todo)} 张图全部下载失败",
                )
                logger.warning(
                    f"微博 {post_id} -> 群 {gid} 相册「{album_name}」："
                    f"{len(todo)} 张图全部下载失败"
                )
                return
            total_mb = sum(path.stat().st_size for _, path in files) / 1048576
            # 记在重负载动作之前：整批卡住或进程崩了的时候，只有这行能说明当时在传多大一批
            logger.info(
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：{len(files)} 张已下载"
                f"（{total_mb:.1f}MB / {dl_sec:.1f}s），开始上传"
            )
            gif_n = sum(1 for _, path in files if path.suffix.lower() == ".gif")
            # 诊断行先于上传落盘刷盘：死机/被 OOM 杀掉后，看它就知道死机前在传
            # 多大一批、当时的并发与预算配置
            await self._diag(
                f"上传 {len(files)} 张（{total_mb:.1f}MB，GIF {gif_n} 张）"
                f"-> 群 {gid} 相册「{album_name}」"
                f"并发={self._int_cfg('album_upload_concurrency', 3)} "
                f"预算={self._float_cfg('album_upload_payload_mb', 32.0):g}MB"
            )
            up_t0 = time.monotonic()
            ok, fails, marks = await self._upload_files(
                nc, gid, album_id, album_name, files
            )
            up_sec = time.monotonic() - up_t0
            if marks:
                await self._remember(gid, album_id, marks)
            summary = (
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：成功 {ok}/{len(files)}"
                f"，上传 {up_sec:.1f}s（{total_mb:.1f}MB，下载 {dl_sec:.1f}s）"
            )
            if dup:
                summary += f"（另有 {dup} 张已传过跳过）"
            if fails:
                summary += f"，失败 {len(fails)} 张"
            logger.info(summary)
            self._record_album(
                ok,
                dup,
                len(fails),
                summary,
            )
            await self._diag(f"上传结束：成功 {ok}/{len(files)} 张，失败 {len(fails)}")
            if fails:
                logger.warning(f"相册上传失败明细: {'; '.join(fails[:5])}")
            if ok and bool(self.config.get("album_notify", False)):
                text = f"📷 已将微博图片 {ok} 张上传到相册「{album_name}」"
                if dup:
                    text += f"（另有 {dup} 张此前已传过，跳过）"
                if fails:
                    text += f"，{len(fails)} 张失败"
                await self._notify(session, text)
        finally:
            # 无论提前返回还是异常，批次目录（含 .part 残件）都不留到下一轮。
            # 整批几十到几百 MB 的删除是阻塞调用，留在事件循环里能把整个 AstrBot
            # 钉住数秒（同批其他群的推送与轮询跟着一起卡），丢线程池去删。
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def _notify(self, session: str, text: str):
        try:
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 相册上传通知发送失败: {e!r}")

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
                "例如：微博添加 1234567890 或 微博添加 https://weibo.com/u/1234567890"
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
        extra = (
            ""
            if nickname
            else "（未能获取昵称，下轮检查会自动补齐，请留意确认 uid 正确）"
        )
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
        rules = self._album_rules()
        lines.append(f"相册绑定：{len(rules)} 条（微博相册列表 查看）")
        yield event.plain_result("\n".join(lines))

    @weibo.command("相册绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_bind(self, event: AstrMessageEvent, uid_text: str = ""):
        """把某博主的微博图片自动上传到本群指定相册（管理员）：微博相册绑定 <uid或链接> <相册名或ID>"""
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在目标群聊中使用该指令（图片将上传到本群相册）")
            return
        uid = _extract_uid(uid_text)
        # 相册名可能含空格。GreedyStr 只存在于框架内部模块（astrbot.core）、未纳入
        # astrbot.api 公开接口，这里按框架同一套分词规则（re.split(r"\s+")）手动取
        # uid 之后的全部剩余参数，语义与 GreedyStr 的 " ".join(剩余参数) 一致
        album_name = ""
        if uid_text:
            tokens = re.split(r"\s+", (event.message_str or "").strip())
            with contextlib.suppress(ValueError):
                album_name = " ".join(tokens[tokens.index(uid_text) + 1 :])
        if not uid or not album_name:
            yield event.plain_result(
                "用法：微博相册绑定 <uid或主页链接> <相册名或ID>\n"
                "例如：微博相册绑定 1234567890 美食图集\n"
                "相册可用 微博列相册 查看名称与 ID"
            )
            return
        self._sync_accounts_from_config()
        if uid not in self.accounts:
            yield event.plain_result(
                f"账号 {uid} 不在监控列表中，请先用 微博添加 添加，或用 微博列表 查看"
            )
            return
        rules = self._album_rules()
        rules[(uid, str(gid))] = album_name
        self.config["album_rules"] = [f"{u}:{g}:{a}" for (u, g), a in rules.items()]
        self.config.save_config()
        name = self.accounts[uid]["name"]
        tip = (
            ""
            if event.unified_msg_origin in self._sessions()
            else "\n注意：本群尚未 微博绑定，推送不成功不会触发图片上传"
        )
        logger.info(f"{PLUGIN_NAME} 相册绑定: {uid} -> 群 {gid} 相册「{album_name}」")
        yield event.plain_result(
            f"已绑定：{name}（{uid}）的微博图片将自动上传到本群相册「{album_name}」{tip}"
        )

    @weibo.command("相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_unbind(self, event: AstrMessageEvent, uid_text: str = ""):
        """解除博主的相册绑定（管理员）：群内使用解除本群，私聊使用解除全部"""
        uid = _extract_uid(uid_text)
        if not uid:
            yield event.plain_result("用法：微博相册移除 <uid>")
            return
        gid = event.get_group_id()
        rules = self._album_rules()
        targets = [
            (u, g) for (u, g) in rules if u == uid and (not gid or g == str(gid))
        ]
        if not targets:
            yield event.plain_result(
                f"账号 {uid} 没有相册绑定记录（微博相册列表 查看）"
            )
            return
        for key in targets:
            rules.pop(key, None)
        self.config["album_rules"] = [f"{u}:{g}:{a}" for (u, g), a in rules.items()]
        self.config.save_config()
        logger.info(f"{PLUGIN_NAME} 相册解绑: {uid} -> {targets}")
        yield event.plain_result(
            f"已解除 {len(targets)} 条绑定（群：{'、'.join(g for _, g in targets)}）"
        )

    @weibo.command("相册列表")
    async def weibo_album_rules_cmd(self, event: AstrMessageEvent):
        """查看所有博主的相册绑定规则"""
        rules = self._album_rules()
        if not rules:
            yield event.plain_result(
                "暂无绑定规则。在目标群内用 微博相册绑定 <uid> <相册名> 绑定"
            )
            return
        bound_gids = {self._parse_umo(s)[2] for s in self._sessions()}
        lines = ["博主相册绑定规则："]
        for idx, ((uid, gid), album) in enumerate(rules.items(), 1):
            name = self.accounts.get(uid, {}).get("name") or uid
            tip = "" if gid in bound_gids else "（该群未绑定推送，不会触发上传）"
            lines.append(f"{idx}. {name}（{uid}）→ 群 {gid} 相册「{album}」{tip}")
        yield event.plain_result("\n".join(lines))

    @weibo.command("列相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_list(self, event: AstrMessageEvent):
        """查看本群的相册列表（名称与 ID，管理员）"""
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在群聊中使用该指令")
            return
        platform, _, _ = self._parse_umo(event.unified_msg_origin)
        yield event.plain_result("正在获取本群相册列表…")
        try:
            nc = await self._album_client(platform)
            albums = await nc.list_albums(str(gid))
        except NapCatError as e:
            yield event.plain_result(f"获取相册列表失败：{e}")
            return
        if not albums:
            yield event.plain_result(
                "本群没有相册（或机器人没权限），请先在 QQ 里创建相册"
            )
            return
        lines = ["本群相册："]
        for idx, a in enumerate(albums, 1):
            aid = pick(a, ALBUM_LIST_ITEM_ID_KEYS)
            name = pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?")
            lines.append(f"{idx}. {name}（ID: {aid}）")
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
            datetime.fromtimestamp(self.last_check_ts, tz=_TZ_CN).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
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
        lines.append(
            f"出网方式：{'代理 ' + _redact_proxy(self._proxy) if self._proxy else '直连'}"
        )
        if self._web_ready:
            lines.append("控制面板：AstrBot 管理面板 - 插件 - 本插件详情页打开")
        else:
            lines.append("控制面板：当前 AstrBot 版本不支持插件页面，未启用")
        yield event.plain_result("\n".join(lines))
