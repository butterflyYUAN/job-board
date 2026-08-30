# -*- coding: utf-8 -*-
"""
从 job_digest.md 解析四家岗位，生成网页。

输出两份：
  1) job_board.html        —— 自包含快照（数据烘焙进 HTML），供本地 file:// 打开 / serve.py 配套。
  2) deploy/index.html     —— 数据驱动公开页：运行时从同目录 jobs_data.json 拉取最新数据，
                             刷新按钮 = 拉取最新 json；每 3 小时自动拉取一次；切回标签页也静默刷新。
                             deploy/jobs_data.json 由本脚本一并写出，部署到任意静态托管即可对外服务。

运行：venv/Scripts/python build_html.py
"""

import os
import re
import json
import html as _html
import datetime

# 统一使用北京时间（UTC+8），避免依赖运行机器时区（GitHub Actions 默认 UTC）
BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
DIGEST = os.path.join(HERE, "job_digest.md")
OUT = os.path.join(HERE, "job_board.html")
DEPLOY = os.path.join(HERE, "deploy")
DEPLOY_INDEX = os.path.join(DEPLOY, "index.html")
DEPLOY_DATA = os.path.join(DEPLOY, "jobs_data.json")

COMP_MAP = {"🔴": "腾讯", "🟠": "美团", "🔵": "字节", "🟢": "蚂蚁"}
COMP_ORDER = ["腾讯", "美团", "字节", "蚂蚁"]
COMP_COLOR = {"腾讯": "#1f8fff", "美团": "#ffb000", "字节": "#2b6cff", "蚂蚁": "#1677ff"}

# 需要跳过的 markdown 折叠标签行（不计入岗位描述）
_SKIP_RE = re.compile(r"</?details|</?summary|显示更多|^\s*_.*_\s*$")


def parse_segment_jobs(seg):
    """解析某公司的段内容，返回岗位 dict 列表。"""
    lines = seg.split("\n")
    jobs = []
    cur = None
    for ln in lines:
        s = ln.strip()
        if _SKIP_RE.search(ln):
            continue
        if ln.lstrip().startswith("【"):
            if cur:
                jobs.append(cur)
            cur = {"head": ln.strip(), "desc": [], "date": "", "url": ""}
        elif cur is not None and ("📅" in ln or "🔗" in ln):
            dm = re.search(r"📅\s*发布：([^\s　]+)", ln)
            if dm:
                cur["date"] = dm.group(1)
            um = re.search(r"🔗\s*(\S+)", ln)
            if um:
                cur["url"] = um.group(1)
        elif cur is not None:
            # s 为空串即「段内空行」= 段落分隔，保留为 \n，从而实现分行+分段展示
            cur["desc"].append(s)
    if cur:
        jobs.append(cur)
    for j in jobs:
        j["desc"] = "\n".join(j["desc"]).strip()
    return jobs


def parse_digest(text):
    headers = list(re.finditer(r"^##\s*([🔴🟠🔵🟢])\s*(.*)$", text, re.M))
    result = {}
    for i, m in enumerate(headers):
        name = COMP_MAP.get(m.group(1))
        if not name:
            continue
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        result[name] = parse_segment_jobs(text[start:end])
    return result


def escape(s):
    return _html.escape(s or "")


# ===== 公开页（数据驱动）的刷新/初始化 JS =====
# 说明：本页是「后端每 3 小时生成的静态快照」。浏览器端无法现场爬取（无 playwright、
# 也跨不过招聘站反爬），因此逻辑是：
#   - 始终显示「更新于 X」+ 相对时间；
#   - 3 小时以内：视为最新快照，刷新只重新拉取快照（无变化则不重绘），提示更新时间；
#   - 超过 3 小时：明确提示「这是静态快照，由后端每 3 小时刷新」，不再出现吓人的报错。
# 真正的「打开/刷新即现场爬取最新」由带后端的 serve.py 提供（见 public-deploy/）。
PUBLIC_JS = r"""
const PUBLIC_PAGE = true;
const AUTO_MS = 6 * 60 * 60 * 1000;   // 每 6 小时自动拉取最新快照（与云端刷新节奏一致）
let currentUpdated = UPDATED0;

function relTime(ts) {
  const u = new Date(('' + ts).replace(/-/g, '/'));
  if (isNaN(u.getTime())) return '';
  const m = Math.max(0, Math.round((Date.now() - u.getTime()) / 60000));
  if (m < 1) return '刚刚';
  if (m < 60) return m + ' 分钟前';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时前';
  return Math.floor(h / 24) + ' 天前';
}

function showStatus() {
  const st = document.getElementById('status');
  if (!st) return;
  const u = new Date(('' + currentUpdated).replace(/-/g, '/'));
  const stale = isNaN(u.getTime()) || (Date.now() - u.getTime()) > AUTO_MS;
  if (stale) {
    st.innerHTML = '📌 当前为云端快照，数据更新于 <b>' + (currentUpdated || '未知')
      + '</b>。本页由 GitHub Actions 每 6 小时云端自动更新；点「刷新」仅拉取最新快照，不现场爬取。';
  } else {
    st.innerHTML = '✅ 已是最新快照，更新于 <b>' + (currentUpdated || '未知')
      + '</b>（' + relTime(currentUpdated) + '）。';
  }
}

async function loadJson(bust) {
  const url = './jobs_data.json' + (bust ? ('?t=' + Date.now()) : '');
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();   // {updated, order, data}
}

async function applyJson(j, force) {
  if (!j || !j.data) return false;
  if (!force && j.updated && j.updated === currentUpdated) return false;  // 无变化则不重绘
  for (const k in j.data) DATA[k] = j.data[k];
  const order = (j.order && j.order.length) ? j.order : Object.keys(j.data);
  ORDER.length = 0;
  order.forEach(function(c) { ORDER.push(c); });
  ORDER.forEach(function(c) { page[c] = 1; });
  if (ORDER.indexOf(current) < 0) current = ORDER[0] || '';
  currentUpdated = j.updated || currentUpdated;
  setUpdated(j.updated);
  renderTabs(); render();
  return true;
}

async function doRefresh() {
  const btn = document.getElementById('refreshBtn');
  const txt = document.getElementById('refreshTxt');
  const st = document.getElementById('status');
  btn.disabled = true; btn.classList.add('spinning'); txt.textContent = '刷新中';
  try {
    const j = await loadJson(true);
    const changed = await applyJson(j, true);
    if (changed) {
      if (st) st.innerHTML = '✅ 已更新为最新（' + (j.updated || '') + '）';
    } else {
      if (st) st.innerHTML = '✅ 已是最新快照，更新于 <b>' + (j.updated || currentUpdated)
        + '</b>（' + relTime(j.updated || currentUpdated) + '）。';
    }
  } catch (e) {
    if (st) st.innerHTML = '⚠️ 获取最新数据失败（' + (e && e.message ? e.message : '')
      + '），当前显示的是最近一次快照（更新于 ' + (currentUpdated || '未知') + '）。';
  }
  btn.disabled = false; btn.classList.remove('spinning'); txt.textContent = '刷新';
  showStatus();
}

// 启动：先尝试拉取最新 json（公开环境生效；file:// 会失败则回退内置快照）
(async function initPage() {
  if (PUBLIC_PAGE || !isFileProtocol()) {
    try { await applyJson(await loadJson(true)); } catch (e) { /* 回退内置数据 */ }
  }
  setUpdated(UPDATED0);
  showStatus();
  renderTabs(); render();
  // 每 6 小时自动拉取一次快照；切回标签页时也静默刷新（均仅在数据更新时重绘）
  setInterval(function() {
    loadJson(true).then(function(j) { applyJson(j); showStatus(); }).catch(function() {});
  }, AUTO_MS);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) loadJson(true).then(function(j) { applyJson(j); showStatus(); }).catch(function() {});
  });
})();
"""

# ===== 自包含快照（本地）的刷新/初始化 JS（连接本地 serve.py 后端）=====
LOCAL_JS = r"""
const PUBLIC_PAGE = false;

async function doRefresh() {
  const btn = document.getElementById('refreshBtn');
  const txt = document.getElementById('refreshTxt');
  const st = document.getElementById('status');
  // 以本地文件方式打开，无法直连本地服务：明确提示
  if (isFileProtocol()) {
    if (st) st.innerHTML = '🔌 检测到以本地文件方式打开，刷新功能不可用。<br>'
      + '请在浏览器地址栏打开 <b>http://127.0.0.1:8000/</b> 以使用实时刷新与重新爬取。';
    return;
  }
  btn.disabled = true;
  btn.classList.add('spinning');
  txt.textContent = '爬取中';
  if (st) st.textContent = '正在重新爬取四家招聘网站，请稍候（约 1–3 分钟）…';
  try {
    await fetch('/api/refresh', { method: 'POST' });
  } catch (e) { /* 提交失败也继续轮询 */ }
  await pollUntilDone(st, btn, txt);   // 只有真正爬完才停转圈
}

async function pollUntilDone(st, btn, txt) {
  let waited = 0;
  let connFail = 0;
  for (let i = 0; i < 120; i++) {           // 最多等约 10 分钟
    await new Promise(function(r) { setTimeout(r, 5000); });
    waited += 5;
    try {
      const r = await fetch('/api/crawl_status');
      const s = await r.json();
      connFail = 0;
      if (!s.running) {                      // 唯一停止信号：服务端确认爬完
        for (const k in s.data) DATA[k] = s.data[k];
        const newOrder = (s.companies && s.companies.length) ? s.companies : ORDER;
        ORDER.length = 0;
        newOrder.forEach(function(c) { ORDER.push(c); });
        ORDER.forEach(function(c) { page[c] = 1; });
        if (ORDER.indexOf(current) < 0) current = ORDER[0] || '';
        setUpdated(s.updated);
        if (st) st.textContent = '已更新为最新（' + (s.updated || '') + '）';
        renderTabs(); render();
        btn.disabled = false; btn.classList.remove('spinning'); txt.textContent = '刷新';
        return;
      }
      if (st) st.textContent = '正在重新爬取四家招聘网站…（已等待 ' + waited + ' 秒，请勿关闭页面）';
    } catch (e) {
      connFail++;
      if (connFail >= 3) {                   // 连续 3 次连不上 = 服务未运行
        if (st) st.innerHTML = '🔌 无法连接本地服务（http://127.0.0.1:8000）。'
          + '请先启动 serve.py，或改用 <b>http://127.0.0.1:8000/</b> 打开本页。';
        btn.disabled = false; btn.classList.remove('spinning'); txt.textContent = '刷新';
        return;
      }
      if (st) st.textContent = '正在连接服务…（已等待 ' + waited + ' 秒）';
    }
  }
  if (st) st.textContent = '爬取耗时较长，可稍后手动刷新页面查看最新数据';
  btn.disabled = false; btn.classList.remove('spinning'); txt.textContent = '刷新';
}

setUpdated(UPDATED0);
renderTabs();
render();
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>大厂研发岗位速递</title>
<style>
  :root {
    --bg:#f5f7fa; --card:#ffffff; --text:#1f2d3d; --sub:#67748a;
    --line:#e4e9f0; --line-soft:#eef1f6;
    --accent:#2f6fed; --accent-2:#5b8def; --accent-green:#1aa260;
    --radius:14px;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"PingFang SC","Microsoft YaHei",-apple-system,
         "Segoe UI",Roboto,"Helvetica Neue",sans-serif; background:var(--bg);
         color:var(--text); -webkit-font-smoothing:antialiased; }

  header { background:linear-gradient(135deg,#2f6fed,#5b8def); color:#fff;
           padding:30px 20px 20px; }
  header h1 { margin:0 0 6px; font-size:22px; letter-spacing:.3px; font-weight:700; }
  header p { margin:0; opacity:.92; font-size:13.5px; font-weight:400; }
  #refreshBtn { position:fixed; top:16px; right:16px; z-index:200; display:inline-flex;
        align-items:center; gap:7px; background:rgba(255,255,255,.18); color:#fff;
        border:1px solid rgba(255,255,255,.6); padding:9px 15px; border-radius:999px;
        font-size:14px; font-weight:600; cursor:pointer; backdrop-filter:blur(6px);
        transition:.2s ease; }
  #refreshBtn:hover { background:rgba(255,255,255,.34); }
  #refreshBtn:disabled { opacity:.7; cursor:default; }
  #refreshBtn:focus-visible { outline:2px solid #fff; outline-offset:2px; }
  #refreshBtn svg { width:16px; height:16px; }
  #refreshBtn.spinning svg { animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  #status { font-size:12.5px; opacity:.92; margin-top:8px; min-height:16px; }

  .tabs { display:flex; flex-wrap:wrap; gap:12px; padding:16px 20px; position:sticky; top:0;
          background:rgba(245,247,250,.92); backdrop-filter:blur(8px); z-index:50;
          border-bottom:1px solid var(--line); }
  .tab { display:inline-flex; align-items:center; gap:9px; border:1.5px solid var(--line-soft);
         background:var(--card); color:var(--sub); padding:9px 16px; border-radius:999px;
         font-size:14.5px; font-weight:600; cursor:pointer; transition:.18s ease; min-height:44px; }
  .tab:hover { border-color:var(--accent); color:var(--text); transform:translateY(-1px);
               box-shadow:0 4px 12px -6px rgba(47,111,237,.25); }
  .tab .dot { width:11px; height:11px; border-radius:50%; flex:0 0 auto; }
  .tab .nm { line-height:1; }
  .tab .cnt { margin-left:2px; background:var(--line-soft); color:var(--sub);
              border-radius:999px; padding:1px 9px; font-size:12px; font-weight:700; }
  .tab.active { border-color:var(--accent); color:var(--accent);
                box-shadow:0 6px 16px -8px rgba(47,111,237,.45); }
  .tab.active .cnt { background:var(--accent); color:#fff; }
  .tab:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

  .updated { font-size:12.5px; color:var(--sub); padding:12px 20px 0; }

  main { max-width:860px; margin:0 auto; padding:10px 16px 70px; }
  .job { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
         padding:16px 18px; margin:14px 0; box-shadow:0 2px 10px -6px rgba(31,45,61,.12);
         transition:.18s ease; }
  .job:hover { box-shadow:0 6px 18px -8px rgba(31,45,61,.2); border-color:var(--accent-2); }
  .job-head { font-weight:700; font-size:15.5px; line-height:1.5; color:var(--text);
              display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
  .new-badge { flex:0 0 auto; background:#fff1e6; color:#ff6b00; font-size:11px;
               font-weight:700; padding:1px 8px; border-radius:999px; border:1px solid #ffd5b0;
               line-height:1.6; }
  .job-desc { color:#3a4655; font-size:13.5px; line-height:1.75; margin:10px 0 0;
             word-break:break-word; }
  .job-desc.clamp { display:-webkit-box; -webkit-line-clamp:6; -webkit-box-orient:vertical;
                    overflow:hidden; }
  .expand-btn { color:var(--accent); font-size:13px; font-weight:600; cursor:pointer;
                margin-top:6px; display:inline-flex; align-items:center; gap:4px; }
  .expand-btn:hover { text-decoration:underline; }
  .expand-btn svg { width:12px; height:12px; }
  .job-meta { font-size:12.5px; color:var(--sub); margin-top:10px; display:flex;
              flex-wrap:wrap; gap:14px; align-items:center; }
  .job-meta .cal { display:inline-flex; align-items:center; gap:5px; }
  .job-meta a { color:var(--accent); text-decoration:none; display:inline-flex;
                align-items:center; gap:4px; font-weight:600; }
  .job-meta a:hover { text-decoration:underline; }
  .pager { display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:8px;
           margin:24px 0 6px; }
  .pager .pbtn { min-width:38px; height:36px; padding:0 10px; border:1.5px solid var(--line);
          background:var(--card); color:var(--sub); border-radius:9px; cursor:pointer;
          font-size:13.5px; font-weight:600; transition:.15s ease; }
  .pager .pbtn:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
  .pager .pbtn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .pager .pbtn:disabled { opacity:.45; cursor:default; }
  .pager .pbtn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .pager .ellipsis { border:none; background:none; cursor:pointer; min-width:auto; padding:0 2px;
                     color:var(--sub); }
  .pager .ellipsis:hover { color:var(--accent); background:var(--line-soft); }
  .pager .jump-input { width:64px; text-align:center; padding:0 4px; }
  .pager .jump-input::-webkit-outer-spin-button,
  .pager .jump-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
  .pager .jump-input { -moz-appearance:textfield; }
  .pager .psize { display:inline-flex; align-items:center; gap:6px; color:var(--sub); font-size:13px; }
  .pager select { height:36px; border:1.5px solid var(--line); border-radius:9px; padding:0 8px;
          background:var(--card); color:var(--text); font-size:13.5px; font-weight:600; cursor:pointer; }
  .pager select:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .pager .info { color:var(--sub); font-size:12.5px; }
  .empty { text-align:center; color:var(--sub); padding:40px; }
  footer { text-align:center; color:var(--sub); font-size:12px; padding:22px; }

  @media (max-width:520px) {
    header h1 { font-size:20px; }
    .tab { font-size:13.5px; padding:8px 13px; }
    main { padding:10px 12px 60px; }
  }
</style>
</head>
<body>
<button id="refreshBtn" aria-label="重新获取最新岗位" onclick="doRefresh()">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3.2-6.86"/><path d="M21 4v5h-5"/></svg>
  <span id="refreshTxt">刷新</span>
</button>
<header>
  <h1>大厂研发岗位速递</h1>
  <p>__SUBTITLE__</p>
  <div id="status">__STATUS__</div>
</header>
<div class="tabs" id="tabs" role="tablist"></div>
<div class="updated" id="updated"></div>
<main id="main" role="tabpanel"></main>
<footer>__FOOTER__</footer>
<script>
const DATA = __DATA__;
const ORDER = __ORDER__;
const COLORS = __COLOR__;
const UPDATED0 = __UPDATED__;
const PAGE_SIZES = [20, 40, 60, 100];
let currentPageSize = PAGE_SIZES[0];   // 用户可切换 20/40/60/100
const page = {};                        // 每家公司各自维护当前页码
ORDER.forEach(c => page[c] = 1);
let current = ORDER[0] || '';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = (s == null) ? '' : s;
  return d.innerHTML;
}

function formatDesc(s) {
  return esc(s).replace(/\n/g, '<br>');
}

function renderTabs() {
  const tabs = document.getElementById('tabs');
  if (!ORDER.length) { tabs.innerHTML = ''; return; }
  tabs.innerHTML = ORDER.map(function(c) {
    const col = (COLORS[c] || '#888');
    const dot = '<span class="dot" style="background:' + col + '"></span>';
    return '<button class="tab' + (c === current ? ' active' : '') + '" role="tab" '
      + 'aria-selected="' + (c === current) + '" data-comp="' + c + '">'
      + dot + '<span class="nm">' + esc(c) + '</span>'
      + '<span class="cnt">' + (DATA[c] || []).length + '</span></button>';
  }).join('');
  tabs.querySelectorAll('.tab').forEach(function(b) {
    b.onclick = function() {
      current = b.getAttribute('data-comp');
      page[current] = 1;
      renderTabs(); render();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
  });
}

function pageList(cur, total) {
  const pages = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i);
    return pages;
  }
  pages.push(1);
  const start = Math.max(2, cur - 1);
  const end = Math.min(total - 1, cur + 1);
  if (start > 2) pages.push('...');
  for (let i = start; i <= end; i++) pages.push(i);
  if (end < total - 1) pages.push('...');
  pages.push(total);
  return pages;
}

function renderPager(comp, cur, totalPages, total) {
  const opts = PAGE_SIZES.map(function(s) {
    return '<option value="' + s + '"' + (s === currentPageSize ? ' selected' : '') + '>' + s + '</option>';
  }).join('');
  const psize = '<span class="psize">每页<select id="psizeSel" aria-label="每页显示数量">'
    + opts + '</select>条</span>';
  if (totalPages <= 1) {
    return '<div class="pager">' + psize
      + '<span class="info">共 ' + total + ' 个岗位</span></div>';
  }
  let btns = '<button class="pbtn" data-pg="prev"' + (cur <= 1 ? ' disabled' : '') + '>‹ 上一页</button>';
  pageList(cur, totalPages).forEach(function(p) {
    if (p === '...') {
      btns += '<button class="pbtn ellipsis" data-pg="jump" data-total="' + totalPages + '" title="跳转页码">…</button>';
    } else {
      btns += '<button class="pbtn' + (p === cur ? ' active' : '') + '" data-pg="' + p + '">' + p + '</button>';
    }
  });
  btns += '<button class="pbtn" data-pg="next"' + (cur >= totalPages ? ' disabled' : '') + '>下一页 ›</button>';
  return '<div class="pager">' + psize + btns
    + '<span class="info">第 ' + cur + ' / ' + totalPages + ' 页，共 ' + total + ' 个</span></div>';
}

function render() {
  const main = document.getElementById('main');
  const comp = current;
  const list = DATA[comp] || [];
  const total = list.length;
  const totalPages = Math.max(1, Math.ceil(total / currentPageSize));
  let cur = page[comp] || 1;
  if (cur > totalPages) cur = totalPages;
  page[comp] = cur;
  if (!total) {
    main.innerHTML = '<div class="empty">该公司暂无岗位数据</div>';
    return;
  }
  const startIdx = (cur - 1) * currentPageSize;
  const slice = list.slice(startIdx, startIdx + currentPageSize);
  let html = '';
  for (let i = 0; i < slice.length; i++) {
    const j = slice[i];
    const idx = startIdx + i;
    const url = (j.url || '').indexOf('http') === 0 ? j.url : '';
    const descId = 'desc-' + comp + '-' + idx;
    const expandId = 'expand-' + comp + '-' + idx;
    let meta = '';
    if (j.date) meta += '<span class="cal">发布：' + esc(j.date) + '</span>';
    if (url) meta += '<a href="' + esc(url) + '" target="_blank" rel="noopener">查看岗位</a>';
    html += '<div class="job">'
      + '<div class="job-head">'
      + (j.is_new ? '<span class="new-badge">🔥 今日新发</span>' : '')
      + esc(j.head) + '</div>';
    if (j.desc) {
      html += '<div class="job-desc clamp" id="' + descId + '">' + formatDesc(j.desc) + '</div>'
        + '<div class="expand-btn" id="' + expandId + '" onclick="toggleDesc(\'' + descId + '\')">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>'
        + '<span>展开全文</span></div>';
    }
    html += (meta ? '<div class="job-meta">' + meta + '</div>' : '')
      + '</div>';
  }
  html += renderPager(comp, cur, totalPages, total);
  main.innerHTML = html;
  // 每页大小切换
  const sel = document.getElementById('psizeSel');
  if (sel) sel.onchange = function() {
    currentPageSize = parseInt(sel.value, 10) || PAGE_SIZES[0];
    page[comp] = 1;
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  // 分页翻页
  main.querySelectorAll('.pbtn[data-pg]').forEach(function(b) {
    b.onclick = function() {
      const pg = b.getAttribute('data-pg');
      if (pg === 'jump') {
        const total = parseInt(b.getAttribute('data-total'), 10);
        const input = document.createElement('input');
        input.type = 'number'; input.min = 1; input.max = total; input.value = cur;
        input.className = 'pbtn jump-input';
        input.setAttribute('aria-label', '跳转到页码');
        b.parentNode.replaceChild(input, b);
        input.focus(); input.select();
        function finish() {
          const v = parseInt(input.value, 10);
          if (!isNaN(v) && v >= 1 && v <= total) page[comp] = v;
          render();
          window.scrollTo({ top: 0, behavior: 'smooth' });
        }
        input.addEventListener('keydown', function(e) {
          if (e.key === 'Enter') finish();
          if (e.key === 'Escape') render();
        });
        input.addEventListener('blur', function() { setTimeout(finish, 120); });
        return;
      }
      if (pg === 'prev') page[comp] = Math.max(1, cur - 1);
      else if (pg === 'next') page[comp] = Math.min(totalPages, cur + 1);
      else page[comp] = parseInt(pg, 10);
      render();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
  });
  initExpandButtons();
}

function initExpandButtons() {
  document.querySelectorAll('.job-desc.clamp').forEach(function(el) {
    const btnId = 'expand-' + el.id.replace('desc-', '');
    const btn = document.getElementById(btnId);
    if (btn) {
      btn.style.display = el.scrollHeight > el.clientHeight + 2 ? 'inline-flex' : 'none';
    }
  });
}

function toggleDesc(descId) {
  const el = document.getElementById(descId);
  const btnId = 'expand-' + descId.replace('desc-', '');
  const btn = document.getElementById(btnId);
  if (!el || !btn) return;
  const collapseIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 15l-6-6-6 6"/></svg>';
  const expandIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  if (el.classList.contains('clamp')) {
    el.classList.remove('clamp');
    btn.innerHTML = collapseIcon + '<span>收起</span>';
  } else {
    el.classList.add('clamp');
    btn.innerHTML = expandIcon + '<span>展开全文</span>';
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function setUpdated(s) {
  document.getElementById('updated').textContent = s ? ('更新于 ' + s) : '';
}

function isFileProtocol() {
  return location.protocol === 'file:';
}

__REFRESH_JS__
</script>
</body>
</html>
"""


def build_payload(data):
    """生成前端 payload；额外计算 is_new（发布日期==今天），用于前端标「🔥 今日新发」。"""
    today = datetime.datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    payload = {}
    for c in COMP_ORDER:
        payload[c] = [
            {"head": j["head"], "desc": j["desc"], "date": j["date"], "url": j["url"],
             "is_new": (j.get("date") == today)}
            for j in data.get(c, [])
        ]
    return payload


def build_html(data, updated="", public=False):
    payload = build_payload(data)
    companies = [c for c in COMP_ORDER if payload.get(c)]
    data_json = json.dumps(payload, ensure_ascii=False)
    comp_json = json.dumps(companies, ensure_ascii=False)
    color_json = json.dumps(COMP_COLOR, ensure_ascii=False)
    updated_json = json.dumps(updated, ensure_ascii=False)
    js = PUBLIC_JS if public else LOCAL_JS
    subtitle = ("腾讯 · 美团 · 字节 · 蚂蚁 — 云端每 6 小时自动更新，本页展示最近一次快照"
                if public else
                "腾讯 · 美团 · 字节 · 蚂蚁 — 按时间排序，重新生成可获取最新")
    status = ("公开快照页 · 数据由 GitHub Actions 每 6 小时云端自动更新（点刷新只拉最新快照，不现场爬取）"
              if public else
              "本地静态快照（点击右上角「刷新」连接本地服务重新爬取）")
    footer = ("数据来源：各公司官方招聘页 · 本页为云端每 6 小时自动生成的快照"
              if public else
              "数据来源：各公司官方招聘页 · 由 job_digest.md 生成本地快照")
    return (PAGE
            .replace("__DATA__", data_json)
            .replace("__ORDER__", comp_json)
            .replace("__COLOR__", color_json)
            .replace("__UPDATED__", updated_json)
            .replace("__PUBLIC__", "true" if public else "false")
            .replace("__REFRESH_JS__", js)
            .replace("__SUBTITLE__", subtitle)
            .replace("__STATUS__", status)
            .replace("__FOOTER__", footer))


def write_data_json(data, updated):
    os.makedirs(DEPLOY, exist_ok=True)
    payload = build_payload(data)
    companies = [c for c in COMP_ORDER if payload.get(c)]
    out = {"updated": updated, "order": companies, "data": payload}
    with open(DEPLOY_DATA, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def main():
    text = open(DIGEST, encoding="utf-8").read()
    data = parse_digest(text)
    for c in COMP_ORDER:
        print(f">>> {c}: 解析到 {len(data.get(c, []))} 个岗位")
    updated = ""
    try:
        mt = os.path.getmtime(DIGEST)
        updated = datetime.datetime.fromtimestamp(mt, tz=BJ_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    # 1) 本地自包含快照
    doc_local = build_html(data, updated, public=False)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc_local)
    print(f">>> 已生成 {OUT}（{len(doc_local)/1024:.1f} KB）")
    # 2) 公开数据驱动页 + jobs_data.json
    os.makedirs(DEPLOY, exist_ok=True)
    doc_pub = build_html(data, updated, public=True)
    with open(DEPLOY_INDEX, "w", encoding="utf-8") as f:
        f.write(doc_pub)
    print(f">>> 已生成 {DEPLOY_INDEX}（{len(doc_pub)/1024:.1f} KB）")
    write_data_json(data, updated)
    print(f">>> 已生成 {DEPLOY_DATA}")


if __name__ == "__main__":
    main()
