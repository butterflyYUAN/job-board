# -*- coding: utf-8 -*-
"""
岗位 digest 通用模块（四家爬虫共用）
====================================
职责：
  1. parse_date      把各种日期字符串/时间戳解析为 date 对象
  2. date_bucket     把日期归到「当日 / 一周内 / 一个月内 / 更早」四个时间桶
  3. sort_jobs       按 (时间桶升序, 桶内日期降序) 排序
  4. render_job      单条岗位格式化：
                       【公司】职位 地点 工作经验要求
                       <空行>
                       工作描述……
                       <空行>
                       📅 发布：YYYY-MM-DD　🔗 岗位网址
  5. render_section  把某家公司(已按日期排序)的岗位渲染成 markdown 段内容，
                     默认只显示 MAX_VISIBLE(20) 条；若总数 > 20，剩余部分用
                     HTML <details> 折叠，提供「显示更多」让用户自行展开。

设计说明：
  - “显示更多”采用标准 HTML <details>/<summary>。它在 Markdown 渲染器(GitHub、
    VS Code 预览、多数 Wiki)与标准浏览器里都能原生折叠展开，无需额外 JS。
    后续若要做成独立网页/按钮交互，只需把 <details> 块换成自定义 UI 即可。
  - 各爬虫只负责“抓取 + 解析成规范化 dict”，渲染/排序/折叠全部交给本模块，
    保证四家输出格式一致。
"""
import datetime
import re

# 每个招聘网址默认显示的岗位数；超过则折叠到“显示更多”
MAX_VISIBLE = 20

# 单条岗位“工作描述”的最大存储长度；绝大多数岗位描述远低于此值，
# 这里仅作为极端异常数据的兜底截断。前端会用 line-clamp 做美观折叠，
# 点击“展开全文”后可查看完整描述。
DESC_LIMIT = 20000

# 时间桶序号：0=当日 1=一周内 2=一个月内 3=更早(或无日期)
BUCKET_LABEL = {0: "当日发布", 1: "一周内", 2: "一个月内", 3: "更早"}


def parse_date(s):
    """把各类日期表示解析为 datetime.date；解析失败返回 None。

    支持：
      - "2026-08-27" / "2026/08/27" / "2026.08.27"
      - "2026-08-27 15:30:00"（取前 10 位）
      - 纯数字时间戳（秒或毫秒）
      - 美团式 "更新于2026/08/27"（调用方先去掉“更新于”再传入）
    """
    if not s:
        return None
    s = str(s).strip()
    if s in ("", "0", "0000-00-00", "00000000", "None", "null"):
        return None
    # 时间戳（纯数字）
    if s.isdigit():
        try:
            v = int(s)
            if len(s) >= 12:      # 毫秒
                v = v / 1000.0
            return datetime.datetime.fromtimestamp(v).date()
        except Exception:
            return None
    # 取前 10 个字符按常见格式尝试
    head = s[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(head, fmt).date()
        except Exception:
            continue
    # 中文格式：2026年08月27日 / 2026年8月27日
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m:
        try:
            return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except Exception:
            pass
    return None


def date_bucket(d, today=None):
    """返回 (桶序号, 日期) 二元组，用于排序键。无日期 -> (3, None)。"""
    if today is None:
        today = datetime.date.today()
    if d is None:
        return (3, None)
    delta = (today - d).days
    if delta < 0:        # 未来日期（异常数据），当作当日处理
        return (0, d)
    if delta == 0:
        return (0, d)
    if delta <= 7:
        return (1, d)
    if delta <= 30:
        return (2, d)
    return (3, d)


def sort_jobs(jobs, today=None):
    """jobs: list[dict]，每个 dict 需含 'date'（原始日期字符串，可为空）。

    返回新的已排序列表：先按时间桶升序（当日>一周内>一个月内>更早），
    同桶内按日期降序（越新越靠前）；无日期的排最后。
    """
    if today is None:
        today = datetime.date.today()

    def key(j):
        d = parse_date(j.get("date"))
        b, dd = date_bucket(d, today)
        # 桶升序；同桶内日期降序（None 记 0，排最后）
        return (b, -(dd.toordinal() if dd else 0))

    return sorted(jobs, key=key)


def render_job(job, company=None):
    """单条岗位格式化。

    输出：
        【公司】职位 地点 工作经验要求
        <空行>
        工作描述……
        <空行>
        📅 发布：YYYY-MM-DD　🔗 岗位网址
    """
    company = company or job.get("company", "")
    title = (job.get("title") or "").strip()
    loc = job.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("text") or ""
    loc = str(loc).strip()
    exp = job.get("experience") or ""
    if isinstance(exp, dict):
        exp = exp.get("name") or exp.get("text") or ""
    exp = str(exp).strip()
    desc = (job.get("description") or "").strip()
    url = (job.get("url") or "").strip()
    d = parse_date(job.get("date"))
    date_str = d.strftime("%Y-%m-%d") if d else ""

    # 标题行：【公司】职位 地点 工作经验要求（地点/经验缺失则省略）
    head = f"【{company}】{title}"
    parts = [p for p in (loc, exp) if p]
    if parts:
        head += " " + " ".join(parts)

    lines = [head, ""]
    if desc:
        # 完整保留岗位描述（含换行），由前端做美观折叠与展开。
        # DESC_LIMIT 仅用于异常超长数据的兜底告警，不影响正常展示。
        if len(desc) > DESC_LIMIT:
            desc = desc[:DESC_LIMIT].rstrip() + "…\n（描述过长，已截断，完整内容请访问岗位链接）"
        lines.append(desc)
        lines.append("")
    meta = []
    if date_str:
        meta.append(f"📅 发布：{date_str}")
    if url:
        meta.append(f"🔗 {url}")
    if meta:
        lines.append("　".join(meta))
    return "\n".join(lines)


def render_section(company, jobs, today=None, max_visible=MAX_VISIBLE, intro=None):
    """把某家公司(未排序也可，内部会排序)的岗位渲染成 markdown 段内容。

    参数：
      company    : 段内每条岗位用的公司名（如「美团」）
      jobs       : list[dict]，含 title/location/experience/description/url/date
      today      : 基准日期（默认今天），用于时间桶
      max_visible: 默认显示条数（默认 20）
      intro      : 段首说明行（可选）

    返回：(markdown 字符串, 总条数)
      - 总数 <= max_visible：全部平铺展示。
      - 总数 >  max_visible：前 max_visible 条平铺，剩余用 <details> 折叠，
        折叠摘要写“显示更多（剩余 N 个岗位）”。
    """
    jobs = sort_jobs(jobs, today)
    n = len(jobs)
    visible = jobs[:max_visible]
    rest = jobs[max_visible:]

    out = []
    if intro:
        out.append(intro)
        out.append("")
    for j in visible:
        out.append(render_job(j, company))
        out.append("")       # 条目间空行

    if rest:
        out.append(f'<details open>\n<summary>显示更多（剩余 {len(rest)} 个岗位）</summary>\n')
        for j in rest:
            out.append(render_job(j, company))
            out.append("")
        out.append("</details>")

    return "\n".join(out).rstrip() + "\n", n


def write_section(digest_path, marker, section_body):
    """把某公司的段内容覆盖写入 job_digest.md（按 marker 定位旧段，保留其它段）。

    marker 形如 "## 🟠 美团社招"（需能唯一定位本段起始的 '## ' 标题行）。
    - 若文件中已有该 marker：仅替换「本段标题 ~ 下一个 '## ' 标题（或文件尾）」之间内容，
      其余公司段保持不变（与写入顺序无关，安全）。
    - 若没有该 marker：插到文件顶部 '# 标题' 之后；无标题则直接放最前。
    """
    try:
        old = open(digest_path, encoding="utf-8").read()
    except FileNotFoundError:
        old = ""
    idx = old.find(marker)
    if idx >= 0:
        # 本段结束 = marker 之后出现的下一个 "\n## " 标题
        rest = old[idx + len(marker):]
        nxt = rest.find("\n## ")
        if nxt >= 0:
            end = idx + len(marker) + nxt
            new_content = old[:idx].rstrip() + "\n" + section_body + "\n" + old[end:]
        else:
            new_content = old[:idx].rstrip() + "\n" + section_body + "\n"
    else:
        title_idx = old.find("# ")
        if title_idx >= 0:
            nl = old.find("\n", title_idx)
            cut = nl if nl >= 0 else len(old)
            new_content = old[:cut].rstrip() + "\n\n" + section_body + "\n" + old[cut:]
        else:
            new_content = section_body + "\n" + old
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return new_content
