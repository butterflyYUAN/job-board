# -*- coding: utf-8 -*-
"""
字节跳动社招岗位爬取（真浏览器方案，绕过接口反爬）
================================================
原理：用 Playwright 开真实 Chrome 加载社招页，等列表渲染后读 DOM。
      比调 API 更稳（天然带浏览器签名，无需登录）。

依赖（一次性，已装可跳过）：
    venv/Scripts/python -m pip install playwright
    （用系统 Chrome，无需 playwright install chromium）

运行：
    venv/Scripts/python bytedance_playwright.py

说明：
  - 仅爬用户给定的字节社招 URL（含 16 个研发类职能 category + 城市 CT_128/CT_45）。
  - 朋友未来接受任何大厂技术/研发岗，故不做职能硬过滤，全部保留；
    FINANCE_HIGHLIGHT 仅把财务/金融相关岗位高亮置顶，方便优先看。
  - 翻页用 URL 的 current 参数（limit=100，约 9 页抓完 820 个岗），按详情链接去重。
"""
import os
import re
import sys
import time
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 渲染/排序/折叠共用模块
from digest_common import parse_date, render_section, write_section

HERE = os.path.dirname(os.path.abspath(__file__))
# 详情缓存：url -> 完整「职位描述+职位要求+加分项」，避免每次刷新都重新抓 800+ 个详情页
DETAIL_CACHE_PATH = os.path.join(HERE, "bytedance_details_cache.json")


def load_detail_cache():
    try:
        with open(DETAIL_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_detail_cache(cache):
    try:
        with open(DETAIL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

LIMIT = 100          # 每页条数（实测 limit 参数生效）
# 安全上限放大到 25 页(约 2500 条)，避免匹配岗位超过原 15 页上限时被截断、
# 导致新发布岗位进不来、数量长期不变。翻到本页无新增即停，不会死循环。
MAX_PAGES = 25       # 安全上限，避免死循环
BYTEDANCE_BASE = (
    "https://jobs.bytedance.com/experienced/position?keywords="
    "&category=6704215862603155720%2C6704215862557018372%2C6704215886108035339"
    "%2C6704215888985327886%2C6704215897130666254%2C6704215956018694411"
    "%2C6704215957146962184%2C6704215958816295181%2C6704215963966900491"
    "%2C6704216109274368264%2C6704216296701036811%2C6704216635923761412"
    "%2C6704217321877014787%2C6704219452277262596%2C6704219534724696331"
    "%2C6938376045242353957&location=CT_128%2CCT_45&project=&type="
    "&job_hot_flag=&current={CURRENT}&limit={LIMIT}&functionCategory=&tag="
)
CARD_SEL = "a[href*='position/']"


def normalize_bytedance(raw, href):
    """把字节卡片原始 inner_text（保留换行）解析为规范化 dict。

    原始结构：
        标题 - 业务线
        城市+雇佣类型[ - 职能]职位 ID：XXXX
        团队介绍：... 1、... 2、...
    说明：列表视图不含“发布日期”与“工作经验要求”，故 date/experience 留空。
    """
    lines = [ln.strip() for ln in (raw or "").split("\n") if ln.strip()]
    title = lines[0].split(" - ", 1)[0].strip() if lines else ""
    loc = ""
    for ln in lines:
        if "职位 ID" in ln or "职位ID" in ln:
            m = re.match(r"^([\u4e00-\u9fa5A-Za-z/]+?)(?:正式|实习|兼职|校招|社招)", ln)
            loc = m.group(1) if m else (ln.split("职位")[0].strip().split()[-1] if ln.split("职位") else "")
            break
    # 描述：职位 ID：<id> 之后的全部内容（折叠空白便于阅读）
    m = re.search(r"职位\s*ID：\S+\s*(.*)", raw, re.S)
    desc = m.group(1).strip() if m else (raw or "").strip()
    desc = re.sub(r"\s+", " ", desc)
    url = href if href.startswith("http") else ("https://jobs.bytedance.com" + href if href else "")
    return {
        "company": "字节",
        "title": title,
        "location": loc,
        "experience": "",          # 列表视图不含经验要求
        "description": desc,
        "date": "",                # 列表视图不含发布日期
        "url": url,
    }


def extract_bytedance_desc(body):
    """从字节详情页 body text 中提取「职位描述 + 职位要求 + 加分项」。"""
    if not body:
        return ""
    idx_desc = body.find("职位描述")
    idx_req = body.find("职位要求")
    if idx_desc < 0 or idx_req < 0 or idx_req <= idx_desc:
        return ""
    parts = []
    # 职位描述
    desc_part = body[idx_desc:idx_req].strip()
    if desc_part:
        parts.append(desc_part)
    # 职位要求：到「加分项」或「投递」或「相关职位」为止
    idx_bonus = body.find("加分项", idx_req)
    idx_apply = body.find("投递", idx_req)
    idx_related = body.find("相关职位", idx_req)
    end = len(body)
    for cand in (idx_bonus, idx_apply, idx_related):
        if cand > idx_req and cand < end:
            end = cand
    req_part = body[idx_req:end].strip()
    if req_part:
        parts.append(req_part)
    # 加分项
    if idx_bonus > idx_req:
        end2 = len(body)
        for cand in (idx_apply, idx_related):
            if cand > idx_bonus and cand < end2:
                end2 = cand
        bonus_part = body[idx_bonus:end2].strip()
        if bonus_part:
            parts.append(bonus_part)
    return "\n\n".join(parts)


def enrich_bytedance_details(jobs):
    """用 Playwright 顺序进入每个字节岗位详情页，抓取完整职位描述/要求/加分项。

    性能优化：详情按 url 缓存到 DETAIL_CACHE_PATH，二次及以后爬取只抓取
    「列表里新增、缓存未命中」的岗位，刷新可秒级完成。

    重要：Playwright 同步 API 与 greenlet 绑定在「创建它的线程」上，不能跨线程
    使用（ThreadPoolExecutor 会抛 `greenlet.error: cannot switch to a different
    thread`）。因此这里必须顺序执行；这也是为什么之前并发版 0/20 全失败。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[WARN] 未安装 playwright，字节岗位仅使用列表页摘要: {e}")
        return jobs

    cache = load_detail_cache()
    targets = []
    for idx, j in enumerate(jobs):
        url = j.get("url")
        if not url:
            continue
        if url in cache and cache[url]:
            # 命中缓存：直接复用完整详情，不启浏览器
            jobs[idx]["description"] = cache[url]
            jobs[idx]["detail_fetched"] = True
        else:
            targets.append((idx, url))

    if not targets:
        print(f">>> 字节详情全部命中缓存（{len(jobs)} 个），跳过浏览器抓取")
        return jobs

    print(f">>> 启动浏览器顺序抓取 {len(targets)} 个字节新岗位详情"
          f"（缓存命中 {len(jobs) - len(targets)} 个）...")
    with sync_playwright() as p:
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
        page = ctx.new_page()   # 复用单页，循环 goto，避免开 800+ 页泄漏内存
        ok = 0
        for seq, (idx, url) in enumerate(targets, 1):
            try:
                # 详情是 SPA，必须等网络空闲（networkidle）内容才渲染完整，
                # 否则用 domcontentloaded 读到的是空壳，extract 返回空、丢「岗位要求」。
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 等「职位描述」文本出现（详情由 JS 渲染），最长 15 秒
                try:
                    page.wait_for_selector("text=职位描述", timeout=15000)
                except Exception:
                    pass
                body = page.inner_text("body")
                full = extract_bytedance_desc(body)
                if full:
                    jobs[idx]["description"] = full
                    jobs[idx]["detail_fetched"] = True
                    cache[url] = full
                    ok += 1
            except Exception as e:
                print(f"[WARN] 字节详情页失败 ({url}): {e}")
            if seq % 50 == 0:
                save_detail_cache(cache)
                print(f">>> 进度 {seq}/{len(targets)}（成功 {ok}）")
        browser.close()
    save_detail_cache(cache)
    total_ok = sum(1 for j in jobs if j.get("detail_fetched"))
    print(f">>> 字节详情抓取完成（本次成功 {ok}/{len(targets)}；累计命中 {total_ok}/{len(jobs)}）")
    return jobs


def main():
    from playwright.sync_api import sync_playwright

    print(">>> 启动真实浏览器加载字节社招页 ...")
    jobs = []          # (title, job_id, desc, url)
    seen = set()
    with sync_playwright() as p:
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
        page = ctx.new_page()
        page.goto(BYTEDANCE_BASE.format(CURRENT=1, LIMIT=LIMIT),
                  wait_until="domcontentloaded", timeout=60000)

        for pg in range(1, MAX_PAGES + 1):
            if pg > 1:
                page.goto(BYTEDANCE_BASE.format(CURRENT=pg, LIMIT=LIMIT),
                          wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            # 滚动确保懒加载渲染
            for _ in range(3):
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(500)
            page.evaluate("window.scrollTo(0,0)")
            page.wait_for_timeout(500)

            cards = page.locator(CARD_SEL).all()
            before = len(jobs)
            for el in cards:
                try:
                    href = el.get_attribute("href") or ""
                    if not href or "/detail" not in href:
                        continue
                    if href.startswith("/"):
                        href = "https://jobs.bytedance.com" + href
                    if href in seen:
                        continue
                    seen.add(href)
                    raw = el.inner_text()
                    jobs.append((raw, href))
                except Exception:
                    continue
            new = len(jobs) - before
            print(f">>> 第 {pg} 页：本页新增 {new} 条，累计 {len(jobs)} 条")
            if new == 0:
                print(">>> 本页无新增，停止翻页。")
                break

        browser.close()

    # 调试：dump 全部卡片原文
    import json as _json
    dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bytedance_debug.json")
    _json.dump(jobs, open(dbg, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f">>> 调试卡片已存 {dbg}；共抓到 {len(jobs)} 条")

    # 统一解析为规范化 dict（标题/地点/描述），交给 digest_common 排序+渲染。
    jobs = [normalize_bytedance(raw, href) for raw, href in jobs]
    print(f">>> 规范化后共 {len(jobs)} 条（字节列表视图不含发布日期/经验）")

    # 进入每个详情页抓取完整「职位描述 + 职位要求 + 加分项」
    jobs = enrich_bytedance_details(jobs)

    today = datetime.date.today().strftime("%Y-%m-%d")
    intro = (f"_按更新时间排序（当日>一周内>一个月内>更早），默认显示 20 个，"
             f"共 {len(jobs)} 个技术/研发岗；超过 20 个可点「显示更多」展开。"
             f"（字节列表页不含发布日期/经验，故按“更早”排列，经验列留空）_")
    body, total = render_section("字节", jobs, intro=intro)

    digest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_digest.md")
    marker = "## 🔵 字节跳动社招"
    write_section(digest_path, marker, f"## 🔵 字节跳动社招（技术/研发岗，{today}）\n\n" + body)
    print(f">>> 已写入（覆盖）字节段到 {digest_path}（显示 {min(total, 20)} 个，共 {total} 个）")


if __name__ == "__main__":
    main()
