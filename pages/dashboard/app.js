/* 微博实时转发 · 控制面板逻辑
 * 通过 AstrBot 插件页面桥接对象与插件后端通信：
 *   window.AstrBotPluginView（新版）/ window.AstrBotPluginPage（旧版）
 * 所有动态内容一律用 textContent 写入，不拼 HTML，防注入。 */

const POLL_MS = 20000;

const KIND_LABEL = {
  push: "推送成功",
  push_fail: "推送放弃",
  album: "相册上传",
  album_fail: "相册失败",
  risk: "风控冷却",
  error: "检查异常",
  info: "事件",
};

const els = {};
for (const id of document.querySelectorAll("[id]")) els[id.id] = id;

/* ---------- 工具 ---------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== "") node.textContent = text;
  return node;
}

function relTime(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) {
    const future = -diff;
    if (future < 60) return "1 分钟内";
    if (future < 3600) return `${Math.round(future / 60)} 分钟后`;
    if (future < 86400) return `${Math.round(future / 3600)} 小时后`;
    return `${Math.round(future / 86400)} 天后`;
  }
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  return `${Math.round(diff / 86400)} 天前`;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dayKey(offsetDays) {
  const d = new Date(Date.now() - offsetDays * 86400000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

let statusTimer = null;
function setStatus(message, tone = "") {
  const node = els.status;
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    node.className = "status-line";
    return;
  }
  if (statusTimer) clearTimeout(statusTimer);
  node.hidden = false;
  node.textContent = message;
  node.className = `status-line ${tone}`.trim();
  if (tone === "ok") statusTimer = setTimeout(() => setStatus(""), 6000);
}

function applyTheme(ctx) {
  const dark = ctx && (ctx.isDark === true || ctx.isDark === "true");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
}

/* ---------- 页内确认框 ----------
 * AstrBot 插件页 iframe 的 sandbox 只有 allow-scripts/forms/downloads，没有
 * allow-modals：window.confirm() 会被浏览器静默拦截并返回 false，导致「移除」
 * 永远无法确认。所有原生弹窗都不能用，确认交互一律走本对话框。 */

function confirmDialog(message) {
  return new Promise((resolve) => {
    const mask = el("div", "modal-mask");
    const box = el("div", "modal");
    box.append(el("div", "modal-title", "请确认操作"));
    box.append(el("div", "modal-body", message));
    const actions = el("div", "modal-actions");
    const cancel = el("button", "btn", "取消");
    const ok = el("button", "btn danger", "确认");
    cancel.type = "button";
    ok.type = "button";
    actions.append(cancel, ok);
    box.append(actions);
    mask.append(box);
    document.body.append(mask);

    const close = (result) => {
      document.removeEventListener("keydown", onKey);
      mask.remove();
      resolve(result);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") close(false);
    };
    document.addEventListener("keydown", onKey);
    // 点击遮罩视为取消；点在对话框内不关闭
    mask.addEventListener("click", (ev) => {
      if (ev.target === mask) close(false);
    });
    cancel.addEventListener("click", () => close(false));
    ok.addEventListener("click", () => close(true));
    ok.focus();
  });
}

/* ---------- 渲染 ---------- */

function tile(label, value, sub, tone) {
  const box = el("div", `tile ${tone || ""}`.trim());
  box.append(el("div", "t-label", label), el("div", "t-value", value));
  if (sub) box.append(el("div", "t-sub", sub));
  return box;
}

function renderStatus(st) {
  const grid = els["status-grid"];
  grid.replaceChildren();

  grid.append(
    tile(
      "轮询任务",
      st.poll_running ? (st.checking ? "运行中 · 正在检查" : "运行中") : "已停止",
      `间隔 ${st.poll_interval} 秒`,
      st.poll_running ? "ok" : "err"
    ),
    tile(
      "上次检查",
      st.last_check_ts ? relTime(st.last_check_ts) : "尚未检查",
      st.last_check_ts ? fmtTime(st.last_check_ts) : "启动后首轮检查完成后更新"
    ),
    tile(
      "下次检查",
      st.poll_running && st.next_check_ts ? `约 ${relTime(st.next_check_ts)}` : "—",
      "按轮询间隔估算，实际有随机抖动"
    ),
    tile(
      "待推送队列",
      `${st.pending_count} 条`,
      st.pending_retrying > 0 ? `其中 ${st.pending_retrying} 条在重试` : "队列状态健康",
      st.pending_retrying > 0 ? "warn" : ""
    ),
    tile(
      "风控状态",
      st.risk_blocked
        ? `冷却中 · 剩约 ${Math.max(1, Math.round((st.risk_until_ts - Date.now() / 1000) / 60))} 分钟`
        : "正常",
      st.risk_blocked ? "触发 418/432 后的全局冷却" : "无 IP 级风控限制",
      st.risk_blocked ? "warn" : "ok"
    ),
    tile(
      "身份模式",
      st.identity_mode === "custom" ? "自定义 Cookie" : "免 Cookie 游客",
      st.identity_mode === "custom"
        ? `Cookie 键：${(st.identity_cookie_keys || []).join("、") || "—"}`
        : st.visitor_ts
          ? `游客身份 ${relTime(st.visitor_ts)}更新`
          : "游客身份未获取"
    ),
    tile(
      "出网方式",
      st.proxy ? "代理" : "直连",
      st.proxy || "未配置代理",
      ""
    ),
    tile(
      "群相册",
      st.album_enabled ? "开启" : "关闭",
      st.album_enabled
        ? st.ffmpeg
          ? "ffmpeg 可用，实况图可转 GIF"
          : "ffmpeg 缺失，实况图仅传封面静图"
        : "在插件配置面板开启",
      st.album_enabled ? "ok" : ""
    )
  );
}

function renderListEmpty(listEl, emptyEl, count) {
  emptyEl.hidden = count > 0;
}

function renderAccounts(accounts) {
  els["accounts-count"].textContent = String(accounts.length);
  const list = els["account-list"];
  list.replaceChildren();
  for (const acc of accounts) {
    const li = el("li");
    const main = el("div", "li-main");
    main.append(el("span", "", acc.name));
    main.append(el("span", "li-sub", `uid ${acc.uid}`));
    li.append(main);
    if (acc.fail_count > 0) {
      li.append(
        el(
          "span",
          "badge err",
          acc.next_retry_ts > Date.now() / 1000
            ? `连续失败 ${acc.fail_count} 次`
            : `失败 ${acc.fail_count} 次`
        )
      );
    } else if (!acc.baseline_done) {
      li.append(el("span", "badge warn", "待建基线"));
    }
    li.append(el("span", "badge", `已见 ${acc.seen_count}`));
    if (acc.push_ok > 0) li.append(el("span", "badge", `已推 ${acc.push_ok}`));
    const rm = el("button", "btn-mini", "移除");
    rm.type = "button";
    rm.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        `确定取消监控「${acc.name}」（uid ${acc.uid}）吗？`
      );
      if (!confirmed) return;
      act(rm, "accounts", { action: "remove", uid: acc.uid }, () =>
        setStatus(`已取消监控：${acc.name}`, "ok")
      );
    });
    li.append(rm);
    list.append(li);
  }
  renderListEmpty(list, els["accounts-empty"], accounts.length);
}

function renderSessions(sessions) {
  els["sessions-count"].textContent = String(sessions.length);
  const list = els["session-list"];
  list.replaceChildren();
  for (const umo of sessions) {
    const li = el("li");
    const parts = umo.split(":");
    const main = el("div", "li-main");
    if (parts.length === 3) {
      main.append(el("span", "", parts[2]));
      main.append(el("span", "li-sub", `${parts[0]} · ${parts[1]}`));
    } else {
      main.append(el("span", "", umo));
    }
    li.append(main);
    const rm = el("button", "btn-mini", "移除");
    rm.type = "button";
    rm.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        `确定解除该推送目标吗？\n${umo}`
      );
      if (!confirmed) return;
      act(rm, "sessions", { action: "remove", umo }, () => setStatus("已移除推送目标", "ok"));
    });
    li.append(rm);
    list.append(li);
  }
  renderListEmpty(list, els["sessions-empty"], sessions.length);
}

function renderRules(rules) {
  els["rules-count"].textContent = String(rules.length);
  const tbody = els["rule-rows"];
  tbody.replaceChildren();
  for (const rule of rules) {
    const tr = el("tr");
    const nameCell = el("td");
    nameCell.append(el("span", "", rule.name || rule.uid));
    nameCell.append(el("span", "dim", `（${rule.uid}）`));
    tr.append(nameCell, el("td", "", rule.gid), el("td", "", rule.album));
    const op = el("td", "col-op");
    const rm = el("button", "btn-mini", "移除");
    rm.type = "button";
    rm.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        `确定移除该相册绑定吗？\n${rule.name || rule.uid} → 群 ${rule.gid} 相册「${rule.album}」`
      );
      if (!confirmed) return;
      act(
        rm,
        "album-rules",
        { action: "remove", uid: rule.uid, gid: rule.gid },
        () => setStatus("已移除相册绑定规则", "ok")
      );
    });
    op.append(rm);
    tr.append(op);
    tbody.append(tr);
  }
  renderListEmpty(tbody, els["rules-empty"], rules.length);
}

function renderPending(pending, totalCount) {
  els["pending-count"].textContent = String(totalCount);
  const tbody = els["pending-rows"];
  tbody.replaceChildren();
  for (const item of pending) {
    const tr = el("tr");
    tr.append(el("td", "", item.name || item.uid || "—"));
    tr.append(el("td", "dim", item.text || "（无内容）"));
    tr.append(el("td", "dim", item.created_ts ? fmtTime(item.created_ts) : "—"));
    const retry = el("td");
    if (item.retries > 0) retry.append(el("span", "danger", `${item.retries} 次`));
    else retry.append(el("span", "dim", "—"));
    tr.append(retry);
    tbody.append(tr);
  }
  renderListEmpty(tbody, els["pending-empty"], totalCount);
}

function renderStats(stats) {
  const tiles = els["stat-tiles"];
  tiles.replaceChildren();
  const daily = stats.daily || {};
  const today = daily[dayKey(0)] || {};
  const items = [
    { label: "累计推送成功", value: stats.push_ok || 0, cls: "ok" },
    { label: "累计推送放弃", value: stats.push_fail || 0, cls: stats.push_fail ? "err" : "" },
    { label: "今日推送", value: today.push_ok || 0, cls: "" },
    { label: "相册已传图片", value: stats.album_uploaded || 0, cls: "" },
    { label: "相册去重跳过", value: stats.album_skipped || 0, cls: "" },
    { label: "相册失败图片", value: stats.album_failed || 0, cls: stats.album_failed ? "err" : "" },
  ];
  for (const it of items) {
    const box = el("div", `stat-tile ${it.cls}`.trim());
    box.append(el("div", "s-value", String(it.value)), el("div", "s-label", it.label));
    tiles.append(box);
  }

  // 近 7 天柱状图（纯 CSS，同参考项目做法）
  const bars = els["daily-bars"];
  bars.replaceChildren();
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const key = dayKey(i);
    days.push({ key, value: (daily[key] || {}).push_ok || 0 });
  }
  const max = Math.max(...days.map((d) => d.value), 1);
  for (const d of days) {
    const col = el("div", "bar-col");
    col.append(el("div", "bar-num", d.value > 0 ? String(d.value) : ""));
    const track = el("div", "bar-track");
    const bar = el("div", `bar ${d.value ? "" : "zero"}`.trim());
    bar.style.height = `${Math.max((d.value / max) * 100, 2)}%`;
    bar.title = `${d.key}：推送成功 ${d.value} 条`;
    track.append(bar);
    col.append(track);
    col.append(el("div", "bar-label", d.key.slice(5).replace("-", "/")));
    bars.append(col);
  }

  // 博主排行 Top5
  const ranking = els.ranking;
  ranking.replaceChildren();
  const entries = Object.entries(stats.by_uid || {})
    .map(([uid, info]) => ({ uid, name: info.name || uid, count: info.push_ok || 0 }))
    .filter((e) => e.count > 0)
    .sort((a, b) => b.count - a.count)
    .slice(0, 5);
  for (const e of entries) {
    const li = el("li");
    li.append(el("span", "", e.name));
    li.append(el("span", "li-sub", `uid ${e.uid} · ${e.count} 条`));
    ranking.append(li);
  }
  els["ranking-empty"].hidden = entries.length > 0;
}

function renderActivity(activity) {
  const list = els["activity-list"];
  list.replaceChildren();
  const items = [...activity].reverse(); // 最新在前
  for (const act of items) {
    const li = el("li");
    li.append(el("div", `act-dot ${act.kind || "info"}`));
    const body = el("div", "act-body");
    const line = el("div", "act-line");
    line.append(el("span", `act-kind ${act.kind || "info"}`, KIND_LABEL[act.kind] || "事件"));
    if (act.name) line.append(el("span", "act-name", act.name));
    if (act.detail) line.append(el("span", "act-detail", act.detail));
    body.append(line);
    if (act.text) body.append(el("div", "act-text", act.text));
    li.append(body);
    li.append(el("span", "act-time", relTime(act.ts)));
    list.append(li);
  }
  els["activity-empty"].hidden = items.length > 0;
}

function renderOverview(data) {
  if (data.version) {
    els["version-chip"].hidden = false;
    els["version-chip"].textContent = data.version;
  }
  renderStatus(data.status || {});
  renderAccounts(data.accounts || []);
  renderSessions(data.sessions || []);
  renderRules(data.album_rules || []);
  renderPending(data.pending || [], (data.status || {}).pending_count || 0);
  renderStats(data.stats || {});
  renderActivity(data.activity || []);
  els["album-disabled-tip"].hidden = !!(data.status || {}).album_enabled;
}

/* ---------- 数据加载与操作 ---------- */

async function load() {
  try {
    const data = await window.__bridge.apiGet("overview");
    renderOverview(data);
  } catch (e) {
    setStatus(`加载失败：${e && e.message ? e.message : e}`, "error");
  }
}

async function act(btn, endpoint, body, onOk) {
  busy(btn, true);
  try {
    await window.__bridge.apiPost(endpoint, body);
    if (onOk) onOk();
    await load();
  } catch (e) {
    setStatus(`操作失败：${e && e.message ? e.message : e}`, "error");
  } finally {
    busy(btn, false);
  }
}

function wireForms() {
  els["account-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["account-input"];
    const uid = input.value.trim();
    if (!uid) return;
    act(ev.submitter, "accounts", { action: "add", uid }, () => {
      setStatus(`已添加监控：${uid}。下一轮检查将先建立基线，不推历史微博。`, "ok");
      input.value = "";
    });
  });

  els["session-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["session-input"];
    const umo = input.value.trim();
    if (!umo) return;
    act(ev.submitter, "sessions", { action: "add", umo }, () => {
      setStatus("已添加推送目标", "ok");
      input.value = "";
    });
  });

  els["rule-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const body = {
      action: "add",
      uid: els["rule-uid"].value.trim(),
      gid: els["rule-gid"].value.trim(),
      album: els["rule-album"].value.trim(),
    };
    if (!body.uid || !body.gid || !body.album) {
      setStatus("添加相册规则需要填写 uid、群号、相册名三项", "warning");
      return;
    }
    act(ev.submitter, "album-rules", body, () => {
      setStatus("已添加相册绑定规则", "ok");
      els["rule-uid"].value = "";
      els["rule-gid"].value = "";
      els["rule-album"].value = "";
    });
  });

  els["btn-check"].addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true);
    window.__bridge
      .apiPost("check-now", {})
      .then(() => {
        setStatus("已触发检查，完成后会出现在「最近动态」里", "ok");
        setTimeout(load, 3000);
      })
      .catch((e) => setStatus(`触发失败：${e && e.message ? e.message : e}`, "error"))
      .finally(() => busy(btn, false));
  });

  els["btn-refresh"].addEventListener("click", () => load());
}

/* ---------- 初始化 ---------- */

async function init() {
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  if (!bridge) {
    setStatus(
      "未检测到 AstrBot 桥接环境：请在 AstrBot WebUI 的「插件 - 微博实时转发 - 详情页」中打开本页面",
      "error"
    );
    return;
  }
  window.__bridge = bridge;

  try {
    const ctx = await bridge.ready();
    applyTheme(ctx);
    if (typeof bridge.onContext === "function") {
      bridge.onContext(applyTheme);
    }
  } catch (e) {
    // 主题读取失败不影响面板功能
  }

  wireForms();
  await load();
  // 页面切到后台时暂停轮询，切回前台时立即刷新一次
  setInterval(() => {
    if (!document.hidden) load();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
}

init();
