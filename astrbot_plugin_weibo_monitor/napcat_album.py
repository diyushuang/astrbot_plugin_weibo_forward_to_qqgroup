"""NapCat 群相册客户端：封装 NapCat 的 OneBot 扩展相册接口。

传输只有一条路：复用 AstrBot 与 NapCat 已有的那条 OneBot 连接，由调用方注入 caller。
地址、token 这些都是 AstrBot 适配器该管的事，插件不另配一份。

接口契约以 NapCatQQ 源码为准（packages/napcat-onebot/action/router.ts、extends/*）：
- get_qun_album_list          {group_id:str, attach_info?:str} -> {album_list, attach_info, has_more}
- upload_image_to_qun_album   {group_id:str, album_id:str, album_name:str, file:str} -> 无 data
- get_group_album_media_list  {group_id:str, album_id:str, attach_info:str} -> {media_list, has_more}

接口名里 qun/group 混用是上游现状，不是笔误；四个参数全是 String，传数字会被 schema 拒掉。
"""

import asyncio
import base64
import random
import re
from pathlib import Path

ALBUM_LIST_ITEM_ID_KEYS = ("album_id", "albumId", "albumIdB64", "id", "bmpno")
ALBUM_LIST_ITEM_NAME_KEYS = ("album_name", "albumName", "name", "title")
# 相册条目的"名字"字段：QQ 相册里显示的文件名就来自这里，去重要靠它。
# NapCat 对这个接口没有声明返回类型（ReturnSchema = Type.Any），只能多键兜。
MEDIA_NAME_KEYS = ("name", "fname", "caption", "fileName", "file_name", "description")

# "协议端没有这个接口"的真实文案：NapCat 对未实现的 action 抛 `不支持的API <action>`(retcode 1404)。
_ACTION_MISSING_HINTS = (
    "不支持的api",
    "unknown api",
    "unknown action",
    "invalid action",
    "no such action",
    "action not exist",
    "method not found",
    "未实现",
    "不存在该接口",
    "无此接口",
)
# 只有"载荷取不到"才值得换下一种载荷方式。
# 注意不能写裸的 "no such"：NapCat 读不到本地文件时抛的正是
# `ENOENT: no such file or directory`，那是 NapCat 与 AstrBot 不同机的正常信号，不是接口缺失。
_FILE_UNUSABLE_HINTS = (
    "no such file",
    "enoent",
    "failed to read file",
    "cannot find the file",
    "not a directory",
    "文件不存在",
    "无法读取",
    "没有那个文件或目录",
)
# NapCat 传相册不是走内核，而是自己 fetch h5.qzone.qq.com 串行发 16KB 分片
# （napcat-core/apis/webapi.ts 的 uploadQunAlbumSlice），非 2xx 就抛
# `HTTP error! status: 502`，OneBot 侧统一包成 retcode=1200。
# 这种是 QQ 相册网关临时挡人，跟插件这边怎么传没关系，只能退避重试。
_UPSTREAM_HINTS = (
    "http error",
    "bad gateway",
    "service unavailable",
    "gateway time",
    "internal server error",
)
# 走 AstrBot 的 OneBot 连接时 NapCat 只会给出 1400/1200/1404 这类码，
# 语义靠 message 判断比靠 retcode 可靠。
_HINT_RULES = (
    (
        _UPSTREAM_HINTS,
        "QQ 相册网关临时报错，NapCat 传分片被挡，等下一批微博再看",
    ),
    (
        ("permission", "forbidden", "not allowed", "权限"),
        "权限不足：请确认机器人在本群被允许上传相册",
    ),
    (("album", "相册"), "相册可能已被删除或 ID 有误，用 微博列相册 重新确认"),
)
# 值得退避重试的瞬时故障：QQ 相册网关抖动 + 连接层面的问题 + 频控。
_RETRYABLE_HINTS = _UPSTREAM_HINTS + (
    "socket hang up",
    "connection reset",
    "connection refused",
    "connection aborted",
    "remote end closed",
    "other side closed",
    "fetch failed",
    "econnreset",
    "etimedout",
    "econnrefused",
    "network",
    "频繁",
    "重试",
    "超时",
    "timeout",
    "frequent",
    "busy",
    "系统繁忙",
    "稍后",
    "网关",
    "网络",
)
# 值得重试的 HTTP 码：5xx 是对方服务器的事，408/429 是"等一下就好"。
# 401/403/404 不在此列——那是 api_root 或 token 配错了，重试只是白等。
_RETRY_STATUS = frozenset({408, 429, *range(500, 600)})
_HTTP_STATUS_RE = re.compile(r"(?:status|code|状态码)\D{0,3}(\d{3})\b", re.I)
# 连不上 NapCat / 等不到响应这一类异常没有响应体可读，只能按类型名认：
# AstrBot 用的是 aiocqhttp 反向 WS（api_timeout_sec=180），超时抛 NetworkError，
# 它继承 IOError，所以 OSError 一并覆盖了 socket 层面的各种断连。
_TRANSPORT_ERRORS = frozenset(
    {
        "NetworkError",
        "HttpFailed",
        "TimeoutError",
        "OSError",
        "ClientError",
        "ServerDisconnectedError",
    }
)


class NapCatError(Exception):
    """带分类标记的错误，供上层决定"换个方式再试"还是"直接放弃"。"""

    def __init__(
        self,
        message: str = "",
        *,
        action_missing: bool = False,
        file_unusable: bool = False,
    ):
        super().__init__(message)
        self.action_missing = action_missing
        self.file_unusable = file_unusable


def _norm(name: str) -> str:
    return re.sub(r"[\s　]+", "", str(name or "")).casefold()


def _has(msg: str, hints: tuple[str, ...]) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in hints)


def pick(d: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        # 只把 None / 空串当缺失：数值 0 是合法取值，吞掉会把 ID 为 0 的条目丢掉
        if v is not None and v != "":
            return str(v)
    return default


def _payload(e: BaseException) -> dict:
    """NapCat 的原始响应体。

    aiocqhttp 1.4 起 ActionFailed 把它挂在 `.result`（1.3 及更早叫 `.info`），
    而 `str(e)` 只剩 `<ActionFailed status='failed', retcode=1200, ...>` 这个壳，
    retcode 与 message 都在响应体里，读错属性就只能把整个壳当文案抛给用户。
    """
    for attr in ("result", "info"):
        v = getattr(e, attr, None)
        if isinstance(v, dict):
            return v
    return {}


def _transient(msg: str, e: BaseException | None = None) -> bool:
    """这条错误是瞬时的吗（QQ 相册网关 5xx、连接抖动、频控）—— 只有这类值得退避重试。"""
    if _has(msg, _RETRYABLE_HINTS):
        return True
    m = _HTTP_STATUS_RE.search(msg or "")
    if m and int(m.group(1)) in _RETRY_STATUS:
        return True
    if e is None:
        return False
    status = getattr(e, "status_code", None)  # aiocqhttp HttpFailed：HTTP 通道的响应码
    if isinstance(status, int):
        return status in _RETRY_STATUS
    return any(c.__name__ in _TRANSPORT_ERRORS for c in type(e).__mro__)


def _classify(text: str) -> tuple[str, bool, bool]:
    """返回 (带提示的文案, 是否接口缺失, 是否载荷不可用)。"""
    missing = _has(text, _ACTION_MISSING_HINTS)
    unusable = _has(text, _FILE_UNUSABLE_HINTS)
    hint = ""
    for keys, wording in _HINT_RULES:
        if _has(text, keys):
            hint = wording
            break
    if missing:
        hint = "协议端没有这个接口，NapCat 需要 v4.8.101 以上"
    return (
        f"{text}" + (f"（{hint}）" if hint and hint not in text else ""),
        missing,
        unusable,
    )


def _fail(action: str, detail: str, code: int = 0, note: str = "") -> NapCatError:
    text, missing, unusable = _classify(detail)
    msg = f"{action} 失败 retcode={code} {text}" if code else f"{action} 失败 {text}"
    return NapCatError(
        f"{msg}；{note}" if note else msg,
        action_missing=missing,
        file_unusable=unusable,
    )


def _b64_payload(raw_b64: bytes) -> str:
    """base64 字节拼成 NapCat 认的载荷串。

    decode 出来的 ASCII 字符串与拼接结果又是两份 1.33 倍图片体积的拷贝（30MB
    的原图各约 40MB）。它和 read_bytes / b64encode 一样是重活，必须一起丢线程池
    ——留在事件循环里，并发上传几路就是每次都把整个 AstrBot 钉住几十到几百毫秒。
    """
    return "base64://" + raw_b64.decode("ascii")


class NapCatAlbum:
    """走调用方注入的 caller —— 也就是 AstrBot 已经和 NapCat 建好的那条 OneBot 连接。"""

    def __init__(
        self,
        caller,
        retries: int = 2,
        preferred: str = "",
        same_host: bool = False,
        backoff: float = 1.0,
    ):
        if caller is None:
            raise NapCatError("拿不到 AstrBot 与 NapCat 之间的连接")
        self.caller = caller
        self.retries = retries
        self.same_host = same_host
        self.backoff = backoff
        # 非空 = 调用方带来了"上次真的传成功过的方式"；空串表示还没学到可用载荷
        self.preferred = preferred if preferred in ("path", "base64") else ""
        self.modes = self._ordered(preferred, same_host)
        # resolve_album 顺手拉到的相册列表，供紧接着的"现选一个"复用（一次性）
        self._last_albums: tuple[str, list[dict]] | None = None

    @staticmethod
    def _ordered(preferred: str, same_host: bool) -> list[str]:
        """same_host 是调用方对"两边共用文件系统"的声明，只有它成立才允许拿本地路径去试。

        路径在 NapCat 那边读不到时抛的是 ENOENT，插件能把载荷降级掉，但 NapCat 控制台
        会实实在在刷一条错误 —— 所以不猜、不问，默认只走 base64。
        preferred 是上次真的传成功过的方式，同机探测过一次就别每张图再撞一遍。
        """
        modes = ["path", "base64"] if same_host else ["base64"]
        if preferred in modes:
            modes.remove(preferred)
            modes.insert(0, preferred)
        return modes

    async def _backoff(self, attempt: int) -> None:
        # 抖动不能省：整批图是并发在传的，撞进同一个 502 窗口的几张会同时退避，
        # 不加抖动它们又会同时撞上去
        base = min(2.0**attempt, 8.0) * self.backoff
        await asyncio.sleep(base + random.uniform(0, 0.5 * self.backoff))

    async def call(self, action: str, *, idempotent: bool = True, **params) -> dict:
        """调一个 action，瞬时故障自己退避重试。

        idempotent=False 表示重复调用会有副作用（上传就是）。这时只有 NapCat 明确回了
        失败响应才重试；连响应都没拿到（反向 WS 等满 180s 就是这种）说明 NapCat 可能还在
        传，盲重试的结果是相册里出现两张一样的图，宁可把这张判失败留给 /补传 补。
        """
        body = {k: v for k, v in params.items() if v is not None}
        last = ""
        for attempt in range(self.retries + 1):
            more = attempt < self.retries
            try:
                res = await self.caller(action, dict(body))
            except Exception as e:  # aiocqhttp ActionFailed / HttpFailed / NetworkError
                info = _payload(e)
                code = info.get("retcode", 0) or 0
                last = (
                    str(info.get("message") or info.get("wording") or "").strip()
                    or str(e).strip()
                    or repr(e)
                )
                # info 非空 = NapCat 回过话，这一张确定没传上去，重试不会传重
                if more and _transient(last, e) and (info or idempotent):
                    await self._backoff(attempt)
                    continue
                note = (
                    ""
                    if info or idempotent
                    else "没拿到 NapCat 的失败响应，这张可能其实已经传上去了，重传前先在相册里确认"
                )
                raise _fail(action, last, code, note) from e
            wrapped = isinstance(res, dict) and "retcode" in res
            j = res if wrapped else {"retcode": 0, "data": res or {}}
            code = j.get("retcode", 0)
            if code != 0 or j.get("status") == "failed":
                # 走到这里说明 NapCat 回话了，失败是确定的，不存在传重风险
                msg = j.get("message") or j.get("wording") or ""
                if more and _transient(msg):
                    last = msg
                    await self._backoff(attempt)
                    continue
                raise _fail(action, msg or str(j)[:160], code)
            return j.get("data") or {}
        raise _fail(action, last or "多次重试后仍然失败")

    async def list_albums(self, group_id: str) -> list[dict]:
        """相册列表（NapCat 单次只给前 10 个，靠 attach_info 翻页）。"""
        out: list[dict] = []
        attach = ""
        for _ in range(20):
            d = await self.call(
                "get_qun_album_list", group_id=str(group_id), attach_info=attach
            )
            raw = d.get("album_list") or d.get("list") or []
            for it in raw:
                if isinstance(it, dict):
                    out.append(it)
                elif it:
                    # 类型声明是 Array<Any>，实测见过对象也见过裸串
                    out.append({"album_id": str(it), "album_name": str(it)})
            nxt = str(d.get("attach_info") or "")
            if not self._more_pages(d, raw, nxt, attach):
                break
            attach = nxt
        return out

    async def list_media(
        self, group_id: str, album_id: str, max_pages: int = 8
    ) -> list[dict]:
        """相册里已有的媒体（该接口没有 count 参数，只能靠 attach_info 翻页）。"""
        items: list[dict] = []
        attach = ""
        for _ in range(max_pages):
            d = await self.call(
                "get_group_album_media_list",
                group_id=str(group_id),
                album_id=str(album_id),
                attach_info=attach,
            )
            page = d.get("media_list") or d.get("mediaList") or d.get("medias") or []
            if isinstance(page, list):
                items += [m for m in page if isinstance(m, dict)]
            nxt = str(d.get("attach_info") or "")
            if not self._more_pages(d, page, nxt, attach):
                break
            attach = nxt
        return items

    @staticmethod
    def _more_pages(d: dict, page, nxt: str, attach: str) -> bool:
        """has_more 在 media_list 的类型声明里并不存在，缺字段时按"还在往前走"判断。"""
        more = d.get("has_more")
        if more is None:
            return bool(page) and bool(nxt) and nxt != attach
        return bool(more) and nxt != attach

    def take_cached_albums(self, group_id: str) -> list[dict] | None:
        """取走 resolve_album 刚顺带拉到的相册列表（一次性，取完即弃）。

        resolve_album 失败时要转去"列出来让用户现选一个"，那一步要的正是同一份
        列表：不传过去就得多列一遍（NapCat 单页只给 10 个，相册多时要翻好几页）。
        只认同一个群、且刚拉过的那一份，用完立即清掉，不会拿到陈旧数据。
        """
        cached, self._last_albums = self._last_albums, None
        if cached and cached[0] == str(group_id):
            return cached[1]
        return None

    async def resolve_album(
        self, group_id: str, want: str, default_name: str = ""
    ) -> tuple[str, str]:
        """把用户给的相册名/ID 解析成 (album_id, album_name)。

        album_name 是要发给 QQ 的 sAlbumName，不是展示用的摆设，所以即使用户直接给了
        ID 也要回查真实名字，不能拿 ID 顶替。default_name 只在列表拉不到、ID 直通时
        兜底展示名，避免把 ID 当名字发给 QQ。
        """
        want = (want or "").strip()
        if not want:
            raise NapCatError("未指定目标相册")
        looks_like_id = bool(re.fullmatch(r"\d{6,}|\d+_[0-9A-Za-z]{4,}", want))
        try:
            albums = await self.list_albums(group_id)
        except NapCatError:
            if looks_like_id:  # 列不出来时至少让 ID 直通，交给协议端裁决
                return want, default_name or want
            raise
        # 存一份给调用方复用：解析失败转"现选一个"时不必再列一遍
        self._last_albums = (str(group_id), albums)
        for a in albums:
            if pick(a, ALBUM_LIST_ITEM_ID_KEYS) == want:
                return want, pick(a, ALBUM_LIST_ITEM_NAME_KEYS, want)
        exact = [
            a
            for a in albums
            if _norm(pick(a, ALBUM_LIST_ITEM_NAME_KEYS)) == _norm(want)
        ]
        loose = [
            a
            for a in albums
            if _norm(want) in _norm(pick(a, ALBUM_LIST_ITEM_NAME_KEYS))
        ]
        hit = (exact or loose or [None])[0]
        if not hit:
            names = (
                "、".join(pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?") for a in albums)
                or "（无）"
            )
            raise NapCatError(
                f"群里没有找到相册「{want}」，现有相册：{names}。请先在 QQ 里手动创建相册"
            )
        aid = pick(hit, ALBUM_LIST_ITEM_ID_KEYS)
        name = pick(hit, ALBUM_LIST_ITEM_NAME_KEYS, want)
        if not aid:
            raise NapCatError(
                f"相册「{name}」缺少 album_id，请改用 微博列相册 里的相册 ID"
            )
        return aid, name

    async def upload_file(
        self, group_id: str, album_id: str, album_name: str, path: Path
    ) -> str:
        """按 self.modes 的顺序试载荷，返回命中的方式。

        默认只有 base64 一种：NapCat 收到它会先落一个 randomUUID 命名的临时文件再传，
        任何部署拓扑都读得到，代价是相册里的文件名不再是微博 pid（去重改由调用方记账）。

        same_host=True 时才把本地路径排到最前 —— 这条只有 NapCat 与 AstrBot 共用文件系统
        才成立，好处是文件名保留 `<pid>.jpg` 且省掉一次 base64 内存放大。插件写不出
        NapCat 那台机器上的路径，所以拿它去猜就是每张图一条 ENOENT：
        裸路径在 NapCat 侧 checkUriType 判成 Unknown、readFileSync 收到空串，
        报 `ENOENT: ... open ''`。猜错不致命（降级到 base64），学到的方式会排到最前，
        一张图探测一次就够。

        载荷惰性构造：path 命中时连文件都不读（只 stat 确认非空），base64 的
        1.33 倍内存放大只在真要用它时才付。
        """
        errs: list[str] = []
        raw: bytes | None = None
        for mode in list(self.modes):
            if mode == "path":
                try:
                    if (await asyncio.to_thread(path.stat)).st_size == 0:
                        raise NapCatError("图片数据为空")
                except OSError as e:
                    raise NapCatError(f"读取待上传图片失败 {path.name}: {e}") from e
                file = str(await asyncio.to_thread(path.resolve))
            else:
                if raw is None:
                    try:
                        # 30MB 的原图读进来再编码都是重活，别卡住事件循环
                        raw = await asyncio.to_thread(path.read_bytes)
                    except OSError as e:
                        raise NapCatError(f"读取待上传图片失败 {path.name}: {e}") from e
                    if not raw:
                        raise NapCatError("图片数据为空")
                raw_b64 = await asyncio.to_thread(base64.b64encode, raw)
                # 原始字节编码完就没用了：base64 载荷一路上要变成好几份内存拷贝
                # （字节、字符串、aiocqhttp 的 JSON 序列化各一份），能早放一份是一份，
                # 并发几路 20MB 级原图时这份省出来的就是小服务器的生死线。
                # 同一批里换载荷重试的场合大不了再读一次盘。
                raw = None
                file = await asyncio.to_thread(_b64_payload, raw_b64)
                del raw_b64
            try:
                await self.call(
                    "upload_image_to_qun_album",
                    idempotent=False,
                    group_id=str(group_id),
                    album_id=str(album_id),
                    album_name=str(album_name or ""),
                    file=file,
                )
            except NapCatError as e:
                errs.append(f"{mode}: {e}")
                # 接口本身不存在，换载荷格式也一样；业务/权限错误同理，直接上抛
                if e.action_missing or not e.file_unusable:
                    raise
                continue
            if self.modes[0] != mode:  # 学到的可用方式排到最前，后面别再撞错误了
                self.modes.remove(mode)
                self.modes.insert(0, mode)
            return mode
        raise NapCatError(" | ".join(errs))
