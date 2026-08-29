# 大厂岗位速递 · GitHub Actions 自动爬取 + Pages 免费托管

用 GitHub Actions 定时在云端跑爬虫，把结果生成静态页部署到 GitHub Pages。
**完全不依赖你自己的电脑**（关机、休眠都不影响），也**不需要买服务器**。

## 效果与边界（先看这个）

| 你的要求 | 本方案是否支持 | 说明 |
|---|---|---|
| 不受本机电脑开关机/休眠影响 | ✅ 完全支持 | 爬取与部署都发生在 GitHub 云端 |
| 每隔几小时自动更新数据 | ✅ 支持 | 默认每 6 小时一次（UTC 0/6/12/18 点，即北京 8/14/20/2 点） |
| 手动点按钮立即重新爬取 | ✅ 支持 | Actions 页面点「Run workflow」即可随时触发 |
| **打开网页的那一刻现场爬取** | ❌ 做不到 | Pages 是静态托管，打开时无法运行 Playwright |

> 「打开瞬间现场爬」在**任何静态托管**上都物理做不到（浏览器端 JS 既没有 Playwright，也跨不过招聘站反爬）。
> 要实现真正的打开即爬，必须有常驻后端 —— 那需要一台服务器（见 `../public-deploy/README.md` 的 VPS 方案）。
> 本方案是免费过渡：数据最多旧一个刷新周期（6 小时）。

## 部署步骤（约 5 分钟）

### 1. 创建 GitHub 仓库
在 https://github.com/new 新建一个仓库，**Visibility 必须选 Public**
（GitHub 免费账号的 Pages 只对公开仓库免费；私有仓库需要 Pro 套餐）。

### 2. 把本目录推送上去
在本目录执行（把 `<用户名>/<仓库名>` 换成你的）：

```bash
cd github-deploy
git init
git add -A
git commit -m "init: 大厂岗位定时爬取"
git branch -M main
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

### 3. 开启 Pages
仓库 → **Settings** → **Pages** → Source 选择 **GitHub Actions**，保存。

### 4. 触发第一次运行
仓库 → **Actions** → 左侧选「定时爬取大厂岗位并部署到 Pages」→ 右侧 **Run workflow** → 绿色按钮确认。

- 首次运行约 **20–40 分钟**（要装 Chromium + 抓腾讯约 400 个岗位详情）
- 之后每次约 **5–10 分钟**（详情缓存命中，只抓新增岗位）

### 5. 访问
完成后打开：**https://\<用户名\>.github.io/\<仓库名\>/**

## 时间与额度

- GitHub 免费账号每月 **2000 分钟** Actions 额度。
- 每 6 小时一次 = 约 120 次/月 × 5–10 分钟 ≈ **600–1200 分钟/月**，在额度内。
- 想更频繁（如每 3 小时）也可，但会接近上限；改 `.github/workflows/crawl.yml` 里的 cron：
  - 每 3 小时：`'0 */3 * * *'`
  - 每 6 小时：`'0 */6 * * *'`（默认）
  - 每天 8 点（北京）：`'0 0 * * *'`
- cron 用的是 **UTC 时间**，北京时间 = UTC + 8。

## 已内置的提速设计

- 目录下三个 `*_details_cache.json`（约 1.9 MB）已随仓库提交，**首次运行就能大量命中缓存**，
  省去从头抓 400 个详情页的时间。
- workflow 另用 `actions/cache` 在每次运行间传递最新缓存，持续保持增量抓取。

## 风险与排查

- **机房 IP 风控**：GitHub Actions 跑在 Azure 数据中心，美团/字节/蚂蚁对数据中心 IP 比家庭宽带严格。
  若某家抓取失败，`run_all.py` 会自动重试一次；仍失败则该家本次不更新（其它家不受影响）。
  建议**先手动 Run workflow 一次**验证四家是否都成功。
- **查看结果**：Actions → 点进某次运行 → 展开 `爬取四家岗位` 步骤，可看到每家抓到的条数与报错。
- **浏览器**：爬虫优先用系统 Chrome，找不到会自动回退 Playwright 自带 chromium，
  所以 Actions 上无需安装 Chrome（已验证回退逻辑存在于全部四个爬虫）。
- **仓库公开**：因为 Pages 免费版要求公开仓库，爬虫代码与缓存文件会公开可见。
  若介意，请改用 VPS 方案（`../public-deploy/`）。

## 目录说明

```
github-deploy/
├── .github/workflows/crawl.yml   # 定时 + 手动触发，爬取 → 生成 → 部署 Pages
├── run_all.py                    # 依次跑四家爬虫（用 sys.executable，跨平台）
├── build_html.py                 # 读 job_digest.md → 生成 deploy/index.html + jobs_data.json
├── digest_common.py              # 排序/渲染/写入 digest 的共用模块
├── job_crawler.py                # 腾讯
├── meituan_playwright.py         # 美团
├── bytedance_playwright.py       # 字节
├── ant_playwright.py             # 蚂蚁
├── requirements.txt              # 仅 playwright
└── *_details_cache.json          # 详情缓存（加速增量抓取）
```

> 本方案只含四家（腾讯/美团/字节/蚂蚁）。京东依赖会过期的登录 Cookie，不适合放在自动化的云端环境。
