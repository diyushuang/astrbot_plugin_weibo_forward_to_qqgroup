# 贡献指南

感谢你愿意为「微博实时转发」出力。本文件是本仓库的**唯一规范出处**：目录约定、代码约定、提交信息格式、分支命名与发版流程都以这里为准。README 讲怎么用，这里讲怎么改。

> **本项目完全由 AI 生成**（见 README 的 [AI 生成声明](README.md#ai-生成声明)）。因此这里对"证据"的要求高于一般开源项目：AI 会犯自信的错，规范的作用就是把每一条结论钉在可复查的证据上。

---

## 1. 开发环境

| 依赖 | 说明 |
| --- | --- |
| Python | 3.10+（`ruff.toml` 的 `target-version` 为 py310），本地开发用 3.13 亦可 |
| ruff | 静态检查与格式化，配置见 `ruff.toml` |
| AstrBot | 仅在真机联调时需要；`bench/` 里的回归脚本已 stub 掉 AstrBot 依赖，无需安装即可运行 |

```bash
ruff check .            # 静态检查
ruff format --check .   # 格式校验（去掉 --check 直接改）
```

> `bench/bench_dashboard.py` 有一个「无 quart 分支」用例，必须在**没装 quart** 的解释器上跑才是全绿；在装了 quart 的环境里它会报 2 项失败，那是该分支的预期表现，不是回归。建议准备两个解释器分别跑。

---

## 2. 目录与文件约定

```
main.py            插件主逻辑：轮询、游客身份、推送链路、取消机制
constants.py       main.py 与 dashboard.py 都要用、且必须取值一致的常量
napcat_album.py    NapCat 群相册 OneBot 扩展客户端
dashboard.py       WebUI 面板后端 API
pages/dashboard/   面板前端（index.html / app.js / style.css）
_conf_schema.json  WebUI 配置面板定义
metadata.yaml      插件元信息（含版本号）
bench/             本地离线回归脚本（被 .gitignore 忽略，不进插件包）
```

**硬约束**

1. **仓库根目录必须就是插件目录。** `metadata.yaml`、`main.py`、`_conf_schema.json` 都在顶层——AstrBot 按目录名加载插件，往下挪一层会直接装不上。
2. **`dashboard.py` 不得反向 `import main`。** AstrBot 以插件目录名为包名加载 `main.py`，`from .main import ...` 会让它被再执行一遍、指令装饰器重复注册。两边都要用的常量放 `constants.py`；`main.py` 需要注入给面板的东西（uid 提取、代理打码）走 `register_dashboard()` 的参数。
3. **`bench/` 不入库。** 它是开发期的验证工具，不随插件分发；但改动推送链路后必须跑通相关用例，并把断言数写进 CHANGELOG。

---

## 3. 代码约定

- **日志**：业务日志一律 `from astrbot.api import logger`，方便被面板的「错误日志留存」Handler 接住。`import logging` 只用于 `_ErrorLogStore` 这类需要 `Handler` 基类的场合。
- **常量归属**：同时被 `main.py` 与 `dashboard.py` 使用的常量放 `constants.py`；只在 `main.py` 用的就近定义在常量区，并写明"为什么是这个值"。
- **措辞必须与真实结果一致**：日志、面板文案、记账口径三者不允许出现"面板写着放弃、群里却有这条微博"这类倒挂。落点不同（群文件 vs 临时文件）就要用不同的措辞。
- **宁可漏，不可重**：发送结果未知（超时、进程被杀）时一律按"可能已送达"处理、不重发。重复推送到群里比漏推更难收拾。
- **注释写"为什么"，不写"做了什么"**：尤其是被实测推翻过的直觉（例如"体积超限导致的拒收"），要把结论和取证方式留在注释里，避免下次又被猜回去。

---

## 4. 测试约定

| 用例 | 覆盖 |
| --- | --- |
| `bench/_test_push_cancel.py` | 面板取消推送：单条取消 / 撤销 / 窗口过期 / 停止本轮 / 发送中竞态 / 持久化往返 |
| `bench/_test_video_chain.py` | 视频直链提取、过窗重取、判死轮数 |
| `bench/_test_video_file_fallback.py` | 视频消息被拒收后的文件兜底（临时 / 永久两种落点） |
| `bench/_test_rich_media_ladder.py` | 摘段升级顺序与拒收回执留档 |
| `bench/_test_pic_reject.py` | 图片拒收与摘段补发 |
| `bench/_test_v163_errorlog.py` | 错误留存与轮转 |
| `bench/test_dashboard_api.py` | 面板 API（含 FakePlugin 复刻） |
| `bench/bench_dashboard.py` | 面板数据装配（有 / 无 quart 两套环境） |

**写用例的规矩**

1. **能加载真 `main.py` 就别手写复刻。** `bench_upload.load_plugin()` 会把插件目录当包加载（相对导入可用），`_test_push_cancel.py` 就是这么测真循环的——条目边界、发送中竞态这类行为，复刻版本根本走不出来。
2. **`test_dashboard_api.py` 的 `FakePlugin` 必须同步。** 它是手写复刻，`main.py` 新增**面板会回调**的业务方法（如 `_reject_reason` / `_video_file_summary` / `push_cancel_undo_seconds`）时，必须补同语义实现，否则面板 API 用例直接 `AttributeError`。
3. **断言数要写进 CHANGELOG。** 每个版本条目里注明本次新增/修改了哪些用例、总数多少，方便回溯"这次改动有没有测试兜着"。

---

## 5. 提交信息规范

采用**中文语义化提交**：首行一句话说清"改了什么、结果如何"，正文用固定小节交代证据与约束。

```
<类型>(<范围>): <一句话结论>

症状：用户或线上看到的现象，附关键报错原文。
根因：确证的原因，并写明**怎么确证的**（实测 / 抓包 / git log -S / 故障注入）。
修复：改了什么，为什么这样改。
已知约束：这次没解决什么、在什么条件下会退化。
测试：断言数与覆盖的用例文件。
版本：vX.Y.Z（含文档/配置类提交时可省略）
```

**类型**（取其一）：`feat` 新功能 / `fix` 修复 / `docs` 文档 / `refactor` 重构 / `perf` 性能 / `test` 测试 / `chore` 构建与杂项。

**范围**（取其一，取最贴近的）：`poll` 微博抓取与轮询 / `push` 推送链路 / `video` 视频转发 / `album` 群相册 / `panel` WebUI 面板 / `docs` 文档 / `repo` 仓库与发布。

**示例**

```
fix(push): 视频段被拒收时改走群文件，视频不再从群里消失

症状：线上告警「视频段被协议端拒收，已去掉视频重发微博 … 的正文」，
      群里只剩正文，视频整条丢失。
根因：rich media transfer failed（retcode 1200）是 NapCat 富媒体通道的通用失败
      回执，被旧口径一律当成"素材超限"。用 statuses/show + Range 请求量出该视频
      真实体积 5.6MB，远低于 100MB 硬限 ⇒ 体积超限不成立，问题在协议端。
修复：命中该回执时把视频改以 upload_group_file 发到每个目标群（另一条上传通道）。
已知约束：群文件上传会占用群文件配额；无上传权限的群仍只能退回链接提示。
测试：448 项断言全过（新增 bench/_test_video_file_fallback.py 48 项）。
版本：v1.7.5
```

**要求**

- 一个版本一个提交；`feat` / `fix` 类提交必须写「症状」与「根因」，根因要能指向证据。
- 不要写"优化代码""修复 bug"这类无信息量的首行。
- **不要擅自 push**，也不要 force push（历史重写仅限维护者按既定流程执行）。

---

## 6. 分支命名

| 分支 | 用途 |
| --- | --- |
| `main` | 唯一长期分支，同时是发布分支：**每个 tag 都打在 `main` 上的某个提交** |
| `feat/<简短描述>` | 新功能，例：`feat/manual-cancel-push` |
| `fix/<简短描述>` | 修复，例：`fix/video-file-fallback` |
| `docs/<简短描述>` | 文档，例：`docs/readme-restructure` |
| `chore/<简短描述>` | 构建、发布、依赖，例：`chore/issue-templates` |

- 描述用小写英文与连字符，不用中文、不用下划线、不堆日期。
- 分支合入 `main` 前必须：`ruff check` 与 `ruff format --check` 通过、相关回归用例通过。
- 本仓库不使用 `develop` / `release` 分支：发布靠 tag 而非分支，避免多一条长期分支带来的同步成本。

---

## 7. 发版流程

**版本号三处必须同步**（漏一处就会出现"面板显示旧版本、CHANGELOG 已发新版"）：

1. `main.py` 的 `PLUGIN_VERSION`（面板页脚显示它）
2. `metadata.yaml` 的 `version`
3. `CHANGELOG.md` 的 `## [x.y.z] - 日期` 标题

提交前用这条命令复核：

```bash
grep -rn "v1\.[0-9]\." --include=*.py --include=*.yaml --include=*.md . | grep -v CHANGELOG
```

**CHANGELOG** 遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，中文条目，固定写「症状 → 根因 → 修复 → 已知约束」，并在末尾附本次测试结论。

**发布步骤**

1. 改版本号三处，写 CHANGELOG 条目；
2. 跑全量回归与 `ruff`，两种 quart 环境的面板基准都过；
3. 提交到 `main`（提交信息按第 5 节格式，正文写清测试结果）；
4. 打**标注 tag**：`git tag -a vX.Y.Z -m "<提交标题>" -m "<日期> · 变更详情见仓库根 CHANGELOG.md"`；
5. 推 `main` 与 tag（`git push origin main && git push origin --tags`）；
6. 在 GitHub 上以该 tag 建 Release，正文取 CHANGELOG 对应段落；
7. 打 zip 包：`astrbot_plugin_weibo_forward_to_qqgroup-vX.Y.Z.zip`，内部放一层同名文件夹（`.zip` 不进仓库）。

> README 顶部版本徽章读的是**最新 tag**（`img.shields.io/github/v/tag/...`），漏打 tag 会让徽章停在旧版本。

---

## 8. 报告问题

- **提 Issue**：用 [Issue 模板](.github/ISSUE_TEMPLATE/) 填写，务必带插件版本（`微博状态` 可查）、AstrBot 版本、协议端与版本，以及面板「错误日志留存」里的对应告警行。
- **提 PR**：先说清"改了什么、怎么验证的"。只改文档也要跑一遍 `ruff`。
- **安全问题**：不要开公开 Issue，走 [SECURITY.md](SECURITY.md) 的私下上报渠道。
