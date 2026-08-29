# -*- coding: utf-8 -*-
"""
一键抓取大厂岗位（腾讯 / 美团 / 字节 / 蚂蚁）
================================================
依次用运行 run_all.py 的解释器跑各爬虫脚本，并内置 Playwright 在 Windows 上
偶发的 access violation（exit -1073741819）重试。

依赖：playwright 已装在 venv（见 setup.sh）。

运行：
    venv/bin/python run_all.py
  （serve.py 会自动调用本脚本；也可手动跑做补爬。）
"""

import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# 用「运行 run_all.py 自身的解释器」去跑各爬虫，保证 Linux / Windows 都可移植，
# 不再写死某台机器的 venv 路径。
PY = sys.executable

# (公司标签, 脚本) —— 公网部署包仅含四家，京东单独成页、不混入此处
STEPS = [
    ("腾讯", "job_crawler.py"),
    ("美团", "meituan_playwright.py"),
    ("字节", "bytedance_playwright.py"),
    ("蚂蚁", "ant_playwright.py"),
]

MAX_RETRY = 2  # 浏览器脚本偶发崩溃时的最大重试次数


def run_one(label, script):
    for attempt in range(1, MAX_RETRY + 2):
        print("\n" + "=" * 56)
        print(f">>> [{label}] 开始抓取（第 {attempt} 次尝试）")
        print("=" * 56)
        try:
            ret = subprocess.run([PY, script], cwd=HERE, check=False)
            if ret.returncode == 0:
                print(f">>> [{label}] 完成 ✅")
                return True
            # 偶发崩溃常见表现为进程被系统杀掉（returncode 负数，如 -1073741819）
            print(f">>> [{label}] 退出码 {ret.returncode}，疑似偶发崩溃，准备重试")
        except Exception as e:
            print(f">>> [{label}] 异常：{e}")
        if attempt <= MAX_RETRY:
            time.sleep(3)
    print(f">>> [{label}] 多次尝试仍失败 ❌")
    return False


def main():
    ok, fail = [], []
    t0 = time.time()
    for label, script in STEPS:
        (ok if run_one(label, script) else fail).append(label)
    cost = int(time.time() - t0)
    print("\n" + "=" * 56)
    print(f"全部完成：成功 {ok or '无'} ；失败 {fail or '无'}")
    print(f"耗时约 {cost} 秒。结果见 job_digest.md")
    print("=" * 56)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
