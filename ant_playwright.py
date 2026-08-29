# -*- coding: utf-8 -*-
"""
蚂蚁集团研发岗爬取（API 直连方案，绕过反爬）
============================================
原理：蚂蚁 off-campus 页是 SPA，列表数据来自
      POST https://hrcareersweb.antgroup.com/api/social/position/search
      该接口带浏览器 ctoken（cookie）即可调用，比纯脚本 POST 更早失败的那版
      （当时没带浏览器上下文、返回 405）稳定得多。
      用 Playwright 开真实 Chrome 拿到 ctoken cookie，再用 page.request 跨域调
      接口，按页抓全量，天然绕过签名/反爬，且无需登录。

数据源（仅以下，符合约束）：
    蚂蚁集团  talent.antgroup.com/off-campus?categories=...&regions=440100,440300,HKG
    -> 映射为 subCategories=130,131,...（技术类整棵子树）+ regions=440100,440300,HKG
    => 即「杭州/广州/深圳/中国香港」的研发/技术类岗位。

依赖（一次性，已装可跳过）：
    venv/Scripts/python -m pip install playwright
    （用系统 Chrome，无需 playwright install chromium）

运行：
    venv/Scripts/python ant_playwright.py

约定（与美团/字节一致）：朋友接受任何大厂技术/研发岗，不做职能硬过滤，全部保留；
      财务/金融相关岗位用 FINANCE_HIGHLIGHT 高亮置顶，方便优先看。
"""
import os
import sys
import json
import math
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 渲染/排序/折叠共用模块
from digest_common import render_section, write_section

# 用户原 URL 的 categories 串（技术类整棵子树）；regions 同原 URL
SUB_CATEGORIES = ("130,131,132,133,134,135,136,137,138,139,140,141,142,176,407,408,409,410,"
                  "411,511,702,703,704,764,769,798,811,100000037,100000053,101300002,"
                  "101300003,101300004,101300005,101300006,101300025,101300034")
REGIONS = "440100,440300,HKG"

PAGE_URL = f"https://talent.antgroup.com/off-campus?categories={SUB_CATEGORIES}&regions={REGIONS}"
API = "https://hrcareersweb.antgroup.com/api/social/position/search"
PAGE_SIZE = 10
MAX_PAGES = 60


def _get_ctoken(page):
    for c in page.context.cookies():
        if "bigfish_ctoken" in c.get("value", ""):
            return c["value"]
    return ""


def _rec(item):
    locs = item.get("workLocations") or []
    cats = item.get("categories") or []
    dept = item.get("departmentPath") or item.get("department") or ""
    pid = item.get("id")
    tid = (item.get("tid") or "").strip()
    name = (item.get("name") or "").strip()
    # 同时保留「岗位职责(description)」和「岗位要求(requirement)」两段，按原换行格式拼接
    duty = (item.get("description") or "").strip()
    req = (item.get("requirement") or "").strip()
    parts = []
    if duty:
        parts.append("岗位职责\n" + duty)
    if req:
        parts.append("岗位要求\n" + req)
    full_desc = "\n\n".join(parts)
    pub = (item.get("publishTime") or "")[:10]
    # 蚂蚁 off-campus 详情页正确地址：off-campus-position?positionId=<id>&tid=<tid>
    # 旧格式 social/position/<id> 已失效、会被重定向回招聘首页，故改为带 tid 的链接。
    if pid and tid:
        url = f"https://talent.antgroup.com/off-campus-position?positionId={pid}&tid={tid}"
    elif pid:
        url = f"https://talent.antgroup.com/off-campus-position?positionId={pid}"
    else:
        url = PAGE_URL
    exp = item.get("experience") or ""
    if isinstance(exp, dict):
        exp = exp.get("name") or exp.get("text") or ""
    return {
        "title": name,
        "city": "/".join(locs),
        "category": "/".join(cats),
        "dept": dept,
        "experience": exp,
        "pub": pub,
        "description": full_desc,
        "url": url,
    }


def main():
    from playwright.sync_api import sync_playwright
    import time as _t

    print(">>> 启动真实浏览器加载蚂蚁 off-campus 页（拿 ctoken + 首请求 body）...")
    jobs = []
    seen = set()
    spa_body = [None]   # SPA 首个搜索请求的原始 body
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

        def on_request(req):
            if "/api/social/position/search" in req.url and spa_body[0] is None:
                spa_body[0] = req.post_data

        page.on("request", on_request)
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        # 等 SPA 首个搜索请求发出并拿到响应（pageSize 由 SPA 决定=10，不修改才不会被风控拒绝）
        page.wait_for_timeout(7000)
        ctoken = _get_ctoken(page)
        if not ctoken:
            print("[WARN] 未拿到 ctoken，重试...")
            page.wait_for_timeout(4000)
            ctoken = _get_ctoken(page)
        print(f">>> ctoken: {ctoken[:30]}...")

        if not spa_body[0]:
            print("[ERROR] 未捕获到 SPA 搜索请求 body，页面可能未初始化。")
            browser.close()
            return
        body = json.loads(spa_body[0])
        # 保留 SPA 原有 pageSize（=10）；改大会被服务端风控拒绝（系统繁忙）
        # 仅通过 pageIndex 翻页。
        # 用 ctoken cookie + 同源头，直连 POST 翻页（仅改 pageIndex）
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://talent.antgroup.com",
            "Referer": PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }

        def post_page(page_index):
            b = dict(body)
            b["pageIndex"] = page_index
            for attempt in range(3):
                try:
                    resp = page.request.post(API + f"?ctoken={ctoken}",
                                             data=json.dumps(b), headers=headers)
                    j = resp.json()
                    if j.get("success"):
                        return j
                except Exception:
                    pass
                _t.sleep(2)
            return {"success": False, "content": []}

        first = post_page(1)
        if not first.get("success"):
            print("[ERROR] 首页接口失败:", first.get("errorMsg"))
            browser.close()
            return
        total = first.get("totalCount") or 0
        page_size = first.get("pageSize") or len(first.get("content") or [10])
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        total_pages = min(total_pages, MAX_PAGES)
        print(f">>> 总数 {total}，每页约 {page_size}，共 {total_pages} 页")

        def collect(j):
            for it in (j.get("content") or []):
                cats = it.get("categories") or []
                # 用户要「研发岗」：仅保留 技术类（技术-开发/算法/前端/测试/运维…）
                if not any(str(c).startswith("技术") for c in cats):
                    continue
                pid = it.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                jobs.append(_rec(it))

        collect(first)
        print(f">>> 第 1 页：{len(first.get('content') or [])} 条，累计 {len(jobs)} 条")
        for pg in range(2, total_pages + 1):
            j = post_page(pg)
            n = len(j.get("content") or [])
            collect(j)
            print(f">>> 第 {pg} 页：{n} 条，累计 {len(jobs)} 条")
            if n == 0:
                break
            _t.sleep(1)
        browser.close()

    print(f">>> 共抓到 {len(jobs)} 条蚂蚁研发岗")

    # 映射为规范化 dict，交给 digest_common 排序+渲染（按发布日期排序）。
    norm = [{
        "company": "蚂蚁",
        "title": it["title"],
        "location": it["city"],
        "experience": it["experience"],
        "description": it["description"],
        "date": it["pub"],
        "url": it["url"],
    } for it in jobs]
    n_dated = sum(1 for j in norm if j["date"])
    print(f">>> 规范化后共 {len(norm)} 条（带发布日期 {n_dated} 条）")

    today = datetime.date.today().strftime("%Y-%m-%d")
    intro = (f"_按更新时间排序（当日>一周内>一个月内>更早），默认显示 20 个，"
             f"共 {len(norm)} 个研发/技术岗（技术类，杭州/广州/深圳/中国香港）；"
             f"超过 20 个可点「显示更多」展开。_")
    body, total = render_section("蚂蚁", norm, intro=intro)

    digest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_digest.md")
    marker = "## 🟢 蚂蚁集团"
    write_section(digest_path, marker, f"## 🟢 蚂蚁集团（研发岗，{today}）\n\n" + body)
    print(f">>> 已写入（覆盖）蚂蚁段到 {digest_path}（显示 {min(total, 20)} 个，共 {total} 个）")


if __name__ == "__main__":
    main()
