#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大厂财务研发岗位每日爬虫
=================================
适用对象：互联网大厂财务系统研发（财务域功能设计 + 开发），准备跳槽。
数据源（仅以下四家，绝不爬取其他站点）：
    1. 腾讯招聘  careers.tencent.com
    2. 字节跳动  jobs.bytedance.com
    3. 美    团  zhaopin.meituan.com
    4. 蚂蚁集团  talent.antgroup.com
输出：匹配「财务/会计/资金/税务/核算/结算/账务/ERP/财务系统…」的岗位列表，
      直接打印，并保存 job_digest.md 便于转发给朋友。

依赖：仅 Python 标准库（urllib / json / time / ssl），无需 pip install。
运行：python job_crawler.py
"""
import json
import time
import os
import sys
import urllib.request
import urllib.parse
import ssl
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 渲染/排序/折叠共用模块
from digest_common import render_section, write_section

SSL_CTX = ssl.create_default_context()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ---------- 匹配关键词（命中任一即视为适合“财务系统研发”） ----------
KEYWORDS = [
    "财务", "会计", "资金", "税务", "核算", "结算", "账务", "财报",
    "财务系统", "财务平台", "财经", "预算", "成本", "出纳", "审计",
    "erp", "finance", "业财",
]

# ================= 1. 腾讯招聘（已验证可用） =================
# Query 接口返回标准 JSON，字段稳定。
TENCENT_API = "https://careers.tencent.com/tencentcareer/api/post/Query"
# 用户原 URL 中 ot_ 类别 -> categoryId；ci_ 城市 -> cityId
TENCENT_CATEGORY = "40001001,40001002,40001003,40001004,40001005,40001006"
TENCENT_CITY = "5,1,37"


def _get_json(url, data=None, headers=None, method="GET"):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_tencent(max_pages=40):
    """翻页抓全量（不再硬卡 6 页=60 条）。

    原为 max_pages=6、pageSize=10 → 最多 60 条，导致匹配岗位总数≥60 时
    数量永远停在 60，今天发布的新岗被挡在第 7 页之后、永远看不到。
    现改为：翻到接口返回不足一整页（末页）或到达安全上限(40页=400条)才停止，
    保证抓到完整集合，新发布的岗位也会进入并按更新时间排序置顶。
    """
    jobs = []
    seen = set()   # 按 PostURL 去重，防止翻页重叠导致重复岗位
    for page in range(1, max_pages + 1):
        qs = urllib.parse.urlencode({
            "timestamp": int(time.time() * 1000),
            "categoryId": TENCENT_CATEGORY,
            "cityId": TENCENT_CITY,
            "pageIndex": page,
            "pageSize": 10,
            "language": "zh-cn",
            "area": "cn",
        })
        try:
            resp = _get_json(TENCENT_API + "?" + qs)
        except Exception as e:
            print(f"[WARN] 腾讯第 {page} 页请求失败: {e}")
            break
        posts = (resp.get("Data") or {}).get("Posts") or []
        if not posts:
            break
        for p in posts:
            u = p.get("PostURL", "")
            if u in seen:
                continue
            seen.add(u)
            jobs.append({
                "company": "腾讯",
                "title": p.get("RecruitPostName", ""),
                "city": p.get("LocationName", ""),
                "bg": p.get("BGName", ""),
                "experience": p.get("RequireWorkYearsName", ""),
                # 列表接口只有岗位职责；下面会用 Playwright 进详情页补齐岗位要求
                "responsibility": (p.get("Responsibility") or "").replace("\r", " ").replace("\n", " "),
                "url": u,
                "update": p.get("LastUpdateTime", ""),
                "date": p.get("LastUpdateTime", ""),
            })
        if len(posts) < 10:
            break
        time.sleep(1)

    # 进入详情页抓取完整「岗位职责 + 岗位要求」，保留原换行格式
    jobs = enrich_tencent_details(jobs)
    return jobs


def _split_duty_req(text):
    """从岗位详情主文本切分「岗位职责/工作职责」与「岗位要求」，保留原换行。

    仅作为兜底：当精确 class（.duty / [class*='require']）未命中时使用，
    降低因页面结构微调导致抓取失败、回退成单行列表摘要的概率。
    """
    import re as _re
    res = {}
    m_duty = _re.search(r"(?:岗位职责|工作职责)\s*\n", text)
    m_req = _re.search(r"岗位要求\s*\n", text)
    if m_duty and m_req:
        sd, sr = m_duty.start(), m_req.start()
        if sd < sr:
            res["duty"] = text[sd:m_req.start()].strip()
            res["req"] = text[m_req.start():].strip()
        else:
            res["req"] = text[sr:m_duty.start()].strip()
            res["duty"] = text[m_duty.start():].strip()
    elif m_duty:
        res["duty"] = text[m_duty.start():].strip()
    elif m_req:
        res["req"] = text[m_req.start():].strip()
    return res


def enrich_tencent_details(jobs):
    """用 Playwright 进入每个腾讯岗位详情页，抓取岗位职责 + 岗位要求。

    性能优化：详情按 PostURL 缓存到 tencent_details_cache.json，二次及以后
    爬取只抓取「列表里新增、缓存未命中」的岗位，刷新可秒级完成（与字节/美团一致）。
    首次全量抓取较慢（约数百个详情页），但仅一次；之后每次只补新岗。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[WARN] 未安装 playwright，腾讯岗位仅使用列表页摘要: {e}")
        return jobs

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tencent_details_cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        cache = {}

    # 先按 url 命中缓存复用完整描述，未命中才进浏览器
    targets = []
    for idx, j in enumerate(jobs):
        u = j.get("url")
        if u and cache.get(u):
            jobs[idx]["responsibility"] = cache[u]
            jobs[idx]["detail_fetched"] = True
        elif u:
            targets.append((idx, u))
    if not targets:
        print(f">>> 腾讯详情全部命中缓存（{len(jobs)} 个），跳过浏览器抓取")
        return jobs

    print(f">>> 启动浏览器抓取 {len(targets)} 个腾讯新岗位详情"
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
        page = ctx.new_page()

        for seq, (idx, url) in enumerate(targets, 1):
            detail = None
            for attempt in range(1, 4):   # 单岗位最多重试 3 次，规避偶发超时/段错误
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)   # 等 SPA 渲染职责/要求
                    # 主策略：精确 class（.duty / 含 require 的容器）
                    duty_el = page.query_selector(".duty")
                    duty = duty_el.inner_text().strip() if duty_el else ""
                    req = ""
                    for el in page.query_selector_all("[class*='require']"):
                        t = el.inner_text().strip()
                        if t.startswith("岗位要求") or "岗位要求" in t[:20]:
                            req = t
                            break
                    # 兜底：精确 class 未抓全时，用主容器 inner_text 正则切分职责/要求
                    if not duty or not req:
                        main_el = page.query_selector("main") or page.query_selector("body")
                        full_txt = main_el.inner_text().strip() if main_el else ""
                        cut = _split_duty_req(full_txt)
                        if not duty and cut.get("duty"):
                            duty = cut["duty"]
                        if not req and cut.get("req"):
                            req = cut["req"]
                        # 仍无任何结构化字段：取主容器全文本（至少保留换行，优于单行列表摘要）
                        if not duty and not req and full_txt:
                            duty = full_txt
                    if duty or req:
                        detail = "\n\n".join([x for x in (duty, req) if x])
                        break
                except Exception as e:
                    print(f"[WARN] 腾讯详情页第 {attempt} 次失败 ({url}): {e}")
                    time.sleep(1.5)
            if detail:
                jobs[idx]["responsibility"] = detail
                jobs[idx]["detail_fetched"] = True
                cache[url] = detail
            else:
                print(f"[WARN] 腾讯详情页 3 次重试均失败，回退列表摘要 ({url})")
            if seq % 10 == 0:
                print(f">>> 已抓 {seq}/{len(targets)} 个腾讯详情")
            time.sleep(0.4)
        browser.close()
    try:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    print(f">>> 腾讯详情抓取完成（本次新抓 {sum(1 for _,_ in targets)}；"
          f"累计命中 {sum(1 for j in jobs if j.get('detail_fetched'))}/{len(jobs)}）")
    return jobs


# ================= 2. 蚂蚁集团（接口已定位，参数待调） =================
# 前端 JS 中已确认存在：/api/campus/position/search 与 /api/social/position/search
# off-campus 页走 campus。POST JSON。实测路径存在但返回 405/HTML，
# 需进一步确认正确 Content-Type 或网关前缀 —— 这里给出实现框架。
ANT_CAMPUS_API = "https://talent.antgroup.com/api/campus/position/search"
ANT_SOCIAL_API = "https://talent.antgroup.com/api/social/position/search"
ANT_CATEGORIES = [131,132,133,134,135,136,137,138,139,140,141,142,176,407,408,409,410,411,
                  511,702,703,704,764,769,798,811,100000037,100000053,101300002,101300003,
                  101300004,101300005,101300006,101300025,101300034]
ANT_REGIONS = ["440100", "440300", "HKG"]


def fetch_ant():
    jobs = []
    body = json.dumps({
        "page": 1, "pageSize": 20,
        "categories": ANT_CATEGORIES, "regions": ANT_REGIONS,
    }).encode("utf-8")
    for api in (ANT_CAMPUS_API, ANT_SOCIAL_API):
        try:
            resp = _get_json(api, data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
            items = resp.get("data") or resp.get("Data") or resp.get("items") or []
            if isinstance(items, dict):
                items = items.get("list") or items.get("records") or []
            for it in items:
                jobs.append({
                    "company": "蚂蚁",
                    "title": it.get("name") or it.get("positionName") or it.get("title") or "",
                    "city": it.get("cityName") or it.get("regionName") or "",
                    "bg": it.get("deptName") or it.get("orgName") or "",
                    "experience": it.get("requireWorkYearsName") or "",
                    "responsibility": (it.get("responsibility") or it.get("description") or "").replace("\n", " "),
                    "url": "https://talent.antgroup.com/off-campus",
                    "update": it.get("publishTime") or it.get("gmtCreate") or "",
                })
            if jobs:
                return jobs
        except Exception as e:
            print(f"[WARN] 蚂蚁接口 {api} 失败（路径已定位，参数/网关待调）: {e}")
    return jobs


# ================= 3. 字节跳动（接口待逆向） =================
# 字节招聘基于飞书 ATS，前端接口有反爬/签名。公开 /api/v2/jobs 返回 404。
# 真实接口需从前端 JS（feishucdn chunk）进一步逆向或补 Header/Token。
BYTEDANCE_API = "https://jobs.bytedance.com/api/v2/jobs"


def fetch_bytedance():
    jobs = []
    try:
        qs = urllib.parse.urlencode({"page": 1, "page_size": 20})
        resp = _get_json(BYTEDANCE_API + "?" + qs)
        items = resp.get("data") or resp.get("Data") or []
        for it in items:
            jobs.append({
                "company": "字节",
                "title": it.get("name") or it.get("job_name") or "",
                "city": it.get("city") or it.get("location_name") or "",
                "bg": it.get("category_name") or "",
                "experience": it.get("experience") or "",
                "responsibility": (it.get("description") or "").replace("\n", " "),
                "url": it.get("url") or it.get("job_url") or "",
                "update": it.get("update_time") or "",
            })
    except Exception as e:
        print(f"[WARN] 字节接口失败（公开路径待逆向，需补 Header/Token）: {e}")
    return jobs


# ================= 4. 美团（已逆向接口，待补真实请求样本） =================
# 逆向结论：真实接口 = POST https://zhaopin.meituan.com/api/job/list
#   请求体字段（来自前端 FilterPositionList chunk）：
#     page{pageNo,pageSize} / jobShareType / keywords / cityList[{code}] /
#     department / jfJgList / jobType[{code,subCode}] / typeCode / specialCode
#   社招 jobType = [{code:"3",subCode:[]}]；城市/职能来自用户原 URL。
# ⚠️ 现状：美团对该接口做了请求校验（反爬/签名），脚本化 POST 目前统一返回
#   {"data":null,"status":1}，无法拿到列表。需从浏览器复制一次真实请求才能跑通
#   （见下方 MEITUAN_COOKIE 注释的“Copy as cURL”步骤）。
MEITUAN_API = "https://zhaopin.meituan.com/api/job/list"
# 用户原 URL 参数：cityList=001019002,001032,001019001  jfJgList=11001_-1
MEITUAN_CITY_LIST = ["001019002", "001032", "001019001"]
MEITUAN_JFJG_LIST = ["11001_-1"]
MEITUAN_COOKIE = "_lxsdk_cuid=19fa79af906c8-05e505fcfd1025-26061d51-144000-19fa79af906c8; _lxsdk=19fa79af906c8-05e505fcfd1025-26061d51-144000-19fa79af906c8; WEBDFPID=zxu0w9v948uy557zz97332253w0x2u0y80uz20zu5xw67958v1z2w0v6-1785522433428-1785223378646WEKYAKGfd79fef3d01d5e9aadc18ccd4d0c95072592; utm_source_rg=AM%2554OM.MO%25335%25P6c.DzUzp5c1NNqPPzqttWWNtD.6Wc.15.cPW.PcN6D-qzN5UxPWD.U-; uuid=d1ae917e400243a3a2ae.1787107129.1.0.0; com.sankuai.recruitment.official.website_strategy=; _lx_utm=utm_source%3DBaidu%26utm_medium%3Dorganic; weixinType=1; weixinCode=091awB0w3p97E73hVl2w3xYHRL0awB0E; unionLoginType=weixin; unionLoginToken=kWpvQsLF4DIP6Hw3aQMnSQlXn/SXQG5hqb3ZcZJbhxbijvOUZr+fDIQF13zV/NVCu5WIxKI85D5mONpSNj9sxmeGO+BE6VBiLMVI988KQhavLQ3U6UtFuYH7DOc30j2cXN1eeXRndgHYSdwjZqj6fS+rKa57sFl504STtPVU0YiTYzSTY/dXIWc5pHXQ1Mi5mPbbGS2H109lTQh2BbWIMgjNwCPguikNNTfYI9Jr+VDd48u3D3KhXvFnrysaBandMbJ+tARCCEaeT1djsMsWtn08L6JeefaImhrGVN/Dwb0QXb1zTiBuQexlyKJMvxaPv+JqV0fWepLZq0gJ1rxAbiWItERjNEKkuo5vzhJiN2ZJXpsQWR8VXDQa6VHdnFHb0HsucKfxGeIgoTKeD5hJOBOReSGDbO67q+QmQPvmWovDp8oSHBgznlvmbPn2uimC9cdIANrvcBm+xSkLVvHyOg==; weixinCodeBack=091awB0w3p97E73hVl2w3xYHRL0awB0E; ssoUnionLoginToken=091awB0w3p97E73hVl2w3xYHRL0awB0E; misId=1495265810; logan_session_token=b2osumnt1z1f78fex3f5; _lxsdk_s=1a03ef0d3d3-ddf-d3d-044%7C%7C21"  # TODO: 登录 zhaopin.meituan.com 后，从浏览器复制 Cookie 粘到这里


def fetch_meituan():
    jobs = []
    if not MEITUAN_COOKIE:
        print("[WARN] 美团：MEITUAN_COOKIE 为空，跳过（填 Cookie 见文件顶部注释）")
        return jobs
    body = json.dumps({
        "page": {"pageNo": 1, "pageSize": 20},
        "jobShareType": "1",
        "keywords": "",
        "cityList": [{"code": c} for c in MEITUAN_CITY_LIST],
        "department": [],
        "jfJgList": [{"code": c} for c in MEITUAN_JFJG_LIST],
        "jobType": [{"code": "3", "subCode": []}],
        "typeCode": [],
        "specialCode": [],
    }, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://zhaopin.meituan.com/web/social",
        "Cookie": MEITUAN_COOKIE,
    }
    try:
        resp = _get_json(MEITUAN_API, data=body, headers=headers, method="POST")
        data = resp.get("data")
        if data is None:
            print("[WARN] 美团：接口返回 data=null。美团对脚本请求做了校验，需从浏览器复制一次真实 job/list 请求(cURL)给我才能跑通。")
            return jobs
        items = data.get("list") or data.get("records") or (data if isinstance(data, list) else [])
        for it in items:
            jobs.append({
                "company": "美团",
                "title": it.get("name") or it.get("positionName") or it.get("postName") or "",
                "city": it.get("cityName") or it.get("city") or "",
                "bg": it.get("deptName") or it.get("bgName") or "",
                "experience": it.get("requireWorkYearsName") or it.get("workYear") or "",
                "responsibility": (it.get("responsibility") or it.get("description") or "").replace("\n", " "),
                "url": it.get("url") or "https://zhaopin.meituan.com/web/social",
                "update": it.get("publishTime") or it.get("gmtCreate") or "",
            })
    except Exception as e:
        print(f"[WARN] 美团接口失败: {e}")
    return jobs


# ================= 匹配 & 输出 =================
def is_match(job):
    text = (job["title"] + " " + job.get("responsibility", "")).lower()
    return any(k.lower() in text for k in KEYWORDS)


def main():
    print("=== 开始抓取腾讯财务/研发岗位 ===")
    raw = fetch_tencent()
    jobs = [{
        "company": "腾讯",
        "title": j["title"],
        "location": j["city"],
        "experience": j["experience"],
        "description": j["responsibility"],
        "date": j["update"],
        "url": j["url"],
    } for j in raw]
    n_dated = sum(1 for j in jobs if j["date"])
    print(f"\n>>> 腾讯共抓到 {len(jobs)} 个岗位（带更新日期 {n_dated} 个）")

    today = datetime.date.today().strftime("%Y-%m-%d")
    intro = (f"_按更新时间排序（当日>一周内>一个月内>更早），每页默认 20 个、可切 20/40/60/100，"
             f"共 {len(jobs)} 个岗位；分页栏可翻页/跳页。_")
    body, total = render_section("腾讯", jobs, intro=intro)

    digest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_digest.md")
    marker = "## 🔴 腾讯（研发岗"
    write_section(digest_path, marker, f"## 🔴 腾讯（研发岗，{today}）\n\n" + body)
    print(f">>> 已写入（覆盖）腾讯段到 {digest_path}（显示 {min(total, 20)} 个，共 {total} 个）")


if __name__ == "__main__":
    main()
