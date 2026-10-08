"""插件内跨模块共享的常量。

只放**main.py 与 dashboard.py 都要用、且必须取值一致**的东西：面板分页上限
与错误留存条数。以前这些值在两个模块各写一份、靠注释提醒"改了要同步"，
改漏一处就会出现"面板说 50 条、实际按 60 条截断"这类对不上的现象。

不反向 import main：AstrBot 以插件目录名为包名加载 main.py，`from .main import`
会把它再执行一遍、指令装饰器重复注册，所以共享内容只能放在两边都不依赖的
中立模块里。dashboard.py 从 main.py 需要的东西（uid 提取、代理打码）走
register_dashboard 的参数注入，同样不构成反向依赖。
"""

# ---------------- 面板分页上限 ----------------
# 面板单次返回的明细条数（待推送队列、最近动态、错误留存）。全量数据都在
# state.json / errors.log 里，面板只展示最近一批，避免大状态文件把响应撑爆。
PENDING_PAGE_LIMIT = 50
ACTIVITY_PAGE_LIMIT = 50
ERRLOG_PAGE_LIMIT = 50

# 内存环形缓冲条数：面板展示上限，也是重启后从 errors.log 尾部回读的条数。
# 与 ERRLOG_PAGE_LIMIT 分开——缓冲要留得比单页多，面板翻页/多次查看时
# 后面的条目还在。
ERRLOG_BUFFER = 200
