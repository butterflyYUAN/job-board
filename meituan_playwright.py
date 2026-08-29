# -*- coding: utf-8 -*-
"""
美团社招岗位爬取（真浏览器方案，绕过接口反爬签名）
================================================
原理：用 Playwright 开一个真实 Chrome，带上登录 Cookie 加载社招页，
      等列表渲染后直接读 DOM。比调 API 更稳（天然带浏览器签名）。

依赖安装（一次性）：
    python -m venv venv
    venv/Scripts/python -m pip install playwright
    venv/Scripts/python -m playwright install chromium

运行：
    venv/Scripts/python meituan_playwright.py

Cookie：复用 job_crawler.py 里的 MEITUAN_COOKIE（你已填过）。
        如果返回“请先登录”，说明 Cookie 过期，去浏览器重新复制即可。
"""
import os
import re
import sys
import json
import datetime
from urllib.parse import urlparse, parse_qs

# 复用 job_crawler 里的配置（注意：job_crawler 只导出 MEITUAN_COOKIE，
# 避免 import 失败把 Cookie 也清空）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from job_crawler import MEITUAN_COOKIE  # noqa
except Exception:
    MEITUAN_COOKIE = ""
# 渲染/排序/折叠共用模块
from digest_common import parse_date, render_section, write_section

MEITUAN_SOCIAL_URL = (
    "https://zhaopin.meituan.com/web/social"
    "?cityList=001019002,001032,001019001&jfJgList=11001_-1"
)
# 说明：jfJgList=11001_-1 是美团「技术」职能大类，天然覆盖技术/研发岗，符合需求。
# 若想扩面（如也含产品/数据等），去社招页手动改职能筛选，把地址栏新 URL 粘到这里即可。

# 职位卡片候选选择器（美团 DOM 类名来自前端 chunk 逆向）
ITEM_SELECTORS = [
    "[data-jobunionid]",
    "a.zp_position",
    ".zp_position",
    "a[href*='position']",
    ".postion_list_wrapper a",
    ".recruitment_position_wrapper a",
]


def normalize_meituan(text, href, duty_text=""):
    """把美团卡片原始文本解析为规范化 dict。

    原始文本示例：
      "无人车业务部-整车可靠性试验工程师 社招 上海市、深圳市 更新于2026/08/27 软硬件服务-无人车业务部 岗位职责 1、负责..."
    解析出：标题 / 地点 / 更新日期 / 工作描述。

    注意：美团列表页仅展示「岗位职责」，不展示「岗位要求」；因此 duty_text 只含岗位职责。
    """
    raw = (text or "").strip()
    title = raw
    loc = ""
    date = ""

    # 1) 去掉“社招”标记，拆分 标题 / 剩余
    if " 社招 " in raw:
        title, rest = raw.split(" 社招 ", 1)
    else:
        rest = raw

    # 2) 更新日期：更新于YYYY/MM/DD
    m = re.search(r"更新于\s*(\d{4}[/\-.]?\d{1,2}[/\-.]?\d{1,2})", rest)
    if m:
        pd = parse_date(m.group(1).replace(".", "-"))
        date = pd.strftime("%Y-%m-%d") if pd else m.group(1).replace("/", "-")

    # 3) 地点：社招 与 更新于 之间的内容
    if " 社招 " in raw and "更新于" in raw:
        loc = raw.split(" 社招 ", 1)[1].split("更新于", 1)[0].strip(" 　")

    # 4) 工作描述：优先用列表页已渲染的完整 .position_duty 文本
    if duty_text:
        desc = duty_text.strip()
    elif "岗位职责" in rest:
        desc = rest.split("岗位职责", 1)[1].strip()
    elif "职责" in rest:
        desc = rest.split("职责", 1)[1].strip()
    else:
        desc = ""
    desc = re.sub(r"^[\s\d．.、)+-]*", "", desc)   # 去掉开头序号/标点

    return {
        "company": "美团",
        "title": title.strip(),
        "location": loc,
        "experience": "",          # 美团卡片文本不含经验要求
        "description": desc,
        "date": date,
        "url": href or "",
    }


def parse_cookie(raw: str):
    cookies = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        for domain in (".zhaopin.meituan.com", ".meituan.com"):
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            })
    return cookies


def enrich_meituan_details(jobs):
    """进每个美团岗位详情页，抓取完整 岗位职责+岗位基本要求(岗位要求)+岗位亮点。

    列表页只暴露「岗位职责」摘要；完整版（含岗位要求 / 岗位亮点）需点进详情页。
    用 jobUnionId -> 完整描述的磁盘缓存，二次刷新只抓新增岗位，速度快。
    """
    if not MEITUAN_COOKIE:
        print("[WARN] 未配置 MEITUAN_COOKIE，美团仅用列表页摘要")
        return jobs
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[WARN] 未安装 playwright，美团仅用列表页摘要: {e}")
        return jobs

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meituan_details_cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        cache = {}

    def ju_of(u):
        try:
            return parse_qs(urlparse(u or "").query).get("jobUnionId", [""])[0]
        except Exception:
            return ""

    targets = [(idx, j.get("url", "")) for idx, j in enumerate(jobs) if j.get("url")]
    if not targets:
        return jobs

    hit = sum(1 for _, u in targets if ju_of(u) in cache)
    print(f">>> 进美团 {len(targets)} 个详情页抓取完整描述（缓存命中 {hit} 个，需抓取 {len(targets) - hit} 个）...")
    ok = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        ctx.add_cookies(parse_cookie(MEITUAN_COOKIE))
        page = ctx.new_page()
        for seq, (idx, url) in enumerate(targets, 1):
            ju = ju_of(url)
            if ju and cache.get(ju):
                full = cache[ju]
                if full and ("职责" in full):
                    jobs[idx]["description"] = full
                    ok += 1
                    continue
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                body = page.inner_text("body")
                i = body.find("岗位职责")
                if i < 0:
                    i = body.find("职位描述")
                if i < 0:
                    continue  # 详情未渲染，保留列表摘要
                full = body[i:]
                # 截断页脚（「一起成长，一起更好」等）
                for footer in ("一起成长", "Grow Together", "相似职位",
                               "相关职位", "相关推荐", "扫码"):
                    j = full.find(footer)
                    if j > 0:
                        full = full[:j]
                        break
                full = full.strip()
                if full:
                    jobs[idx]["description"] = full
                    if ju:
                        cache[ju] = full
                    ok += 1
            except Exception as e:
                print(f"[WARN] 美团详情页失败 ({url}): {e}")
            if seq % 20 == 0:
                page.wait_for_timeout(200)
                print(f">>> 已处理 {seq}/{len(targets)} 个美团详情")
        browser.close()

    try:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    print(f">>> 美团详情抓取完成（成功 {ok}/{len(targets)}）")
    return jobs


def main():
    from playwright.sync_api import sync_playwright

    if not MEITUAN_COOKIE:
        print("[ERROR] MEITUAN_COOKIE 为空，请先在 job_crawler.py 里填好登录 Cookie。")
        return

    print(">>> 启动真实浏览器加载美团社招页 ...")
    jobs = []
    with sync_playwright() as p:
        # 优先用本机已装的 Chrome（跳过 Chromium 下载）；找不到再退回 Playwright 自带 chromium
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
        except Exception as e:
            print(f"[INFO] 未找到系统 Chrome（{e}），改用 Playwright 自带 chromium ...")
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        ctx.add_cookies(parse_cookie(MEITUAN_COOKIE))
        page = ctx.new_page()
        page.goto(MEITUAN_SOCIAL_URL, wait_until="domcontentloaded", timeout=60000)

        # 登录态检查
        try:
            page.wait_for_timeout(2500)
            body_txt = page.inner_text("body")
            if ("登录" in body_txt and "退出" not in body_txt) or "快捷登录" in body_txt:
                print("[WARN] 页面提示登录，Cookie 可能已过期，请重新复制 Cookie。")
        except Exception:
            pass

        # 等待列表渲染
        sel = None
        for cand in ITEM_SELECTORS:
            try:
                page.wait_for_selector(cand, timeout=8000)
                sel = cand
                break
            except Exception:
                continue
        print(f">>> 命中列表选择器: {sel}" if sel else ">>> 未命中预设选择器，将尝试整页提取")

        def scrape_current():
            out = []
            if not sel:
                return out
            for el in page.query_selector_all(sel):
                try:
                    text = el.inner_text().replace("\n", " ").strip()
                    # 优先读取卡片内已渲染的完整岗位职责
                    duty_el = el.query_selector(".position_duty")
                    duty_text = duty_el.inner_text().strip() if duty_el else ""
                    ju = el.get_attribute("data-jobunionid") or ""
                    if ju:
                        href = f"https://zhaopin.meituan.com/web/position/detail?jobUnionId={ju}&highlightType=social"
                    else:
                        href = el.get_attribute("href") or ""
                        if not href:
                            a = el.query_selector("a")
                            if a:
                                href = a.get_attribute("href") or ""
                        if href and href.startswith("/"):
                            href = "https://zhaopin.meituan.com" + href
                except Exception:
                    continue
                if text:
                    out.append((text, href, duty_text))
            return out

        seen = set()
        # 原为 10 页上限，若匹配岗位超过该页量，新发布岗位会被截在窗口外、
        # 数量长期不变。放宽到 40 页（约 800 条）并翻到末页(disabled)才停，
        # 保证抓到完整集合；digest_common 会按更新时间排序，新岗自动置顶。
        max_pages = 40
        for pg in range(max_pages):
            # 滚动触发懒加载
            for _ in range(4):
                page.mouse.wheel(0, 3500)
                page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0,0)")
            before = len(jobs)
            for t, h, d in scrape_current():
                if t not in seen:
                    seen.add(t)
                    jobs.append((t, h, d))
            new = len(jobs) - before
            print(f">>> 第 {pg+1} 页：本页新增 {new} 条，累计 {len(jobs)} 条")
            # 尝试翻页（美团分页器：.mtd-pagination-next，末页 disabled）
            clicked = False
            try:
                nxt_li = page.query_selector(".mtd-pagination-next")
                cls = (nxt_li.get_attribute("class") or "") if nxt_li else ""
                if nxt_li and "mtd-pagination-item-disabled" not in cls:
                    nxt_li.click(timeout=2000)
                    clicked = True
            except Exception:
                pass
            if not clicked:
                break
            page.wait_for_timeout(1500)

        browser.close()

    # 调试：把全部抓到的卡片原文 dump 出来，便于核对选择器/关键词
    dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meituan_debug.json")
    import json as _json
    with open(dbg, "w", encoding="utf-8") as f:
        _json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f">>> 调试卡片已存 {dbg}")

    print(f">>> 共抓到 {len(jobs)} 条职位卡片")

    # 统一解析为规范化 dict（标题/地点/日期/描述），交给 digest_common 排序+渲染。
    # 朋友未来接受任何大厂技术/研发岗，故全部保留，按更新时间排序。
    jobs = [normalize_meituan(text, href, duty) for text, href, duty in jobs]
    # 进详情页抓取完整描述（岗位职责 + 岗位基本要求 + 岗位亮点），覆盖列表页摘要
    jobs = enrich_meituan_details(jobs)
    n_dated = sum(1 for j in jobs if j["date"])
    print(f">>> 规范化后共 {len(jobs)} 条（其中带更新日期 {n_dated} 条）")

    today = datetime.date.today().strftime("%Y-%m-%d")
    intro = (f"_按更新时间排序（当日>一周内>一个月内>更早），默认显示 20 个，"
             f"共 {len(jobs)} 个技术/研发岗；超过 20 个可点「显示更多」展开。_")
    body, total = render_section("美团", jobs, intro=intro)

    digest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_digest.md")
    marker = "## 🟠 美团社招"
    write_section(digest_path, marker, f"## 🟠 美团社招（技术/研发岗，{today}）\n\n" + body)
    print(f">>> 已写入（覆盖）美团段到 {digest_path}（显示 {min(total, 20)} 个，共 {total} 个）")


if __name__ == "__main__":
    main()
