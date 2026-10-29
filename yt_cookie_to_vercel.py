# -*- coding: utf-8 -*-
"""一键把 YouTube cookies.txt 上传到 Vercel 环境变量并重部署。

用法（本目录执行）：
    python yt_cookie_to_vercel.py cookies.txt

cookies.txt 来源：Chrome 装「Get cookies.txt LOCALLY」扩展 → 打开 youtube.com →
导出 Netscape 格式。拿到文件后跑本脚本，等 2 分钟重部署，YouTube 解锁。
"""
import sys
import subprocess
import os

if len(sys.argv) < 2:
    print("用法: python yt_cookie_to_vercel.py <cookies.txt 路径>")
    sys.exit(1)

path = sys.argv[1]
if not os.path.exists(path):
    print("文件不存在:", path)
    sys.exit(1)

raw = open(path, encoding="utf-8", errors="replace").read()
if "# Netscape HTTP Cookie File" not in raw and "# HTTP Cookie File" not in raw:
    print("⚠ 文件不像 Netscape cookies.txt（缺头），仍尝试上传")
if "youtube" not in raw:
    print("⚠ 文件里没有 youtube.com 域的 cookie，请确认是在 youtube.com 页面导出的")
lines = [l for l in raw.splitlines() if l.strip()]
print("读取", len(lines), "行")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
r = subprocess.run(["vercel", "env", "add", "COOKIES", "production"],
                   input=raw, text=True, capture_output=True, encoding="utf-8")
print((r.stdout or "") + (r.stderr or ""))
print("✓ COOKIES 已上传，正在重新部署（约 1 分钟）...")
r = subprocess.run(["vercel", "deploy", "--prod", "--yes"],
                   capture_output=True, text=True, encoding="utf-8")
tail = (r.stdout or "").strip().splitlines()
print(tail[-1] if tail else "deploy done")
print("完成。等 1 分钟后测试 YouTube 下载。")
