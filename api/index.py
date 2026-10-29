# -*- coding: utf-8 -*-
"""
万能视频下载站 · Vercel Serverless 版
架构：解析用 yt-dlp Python API（秒回）；下载用 yt-dlp -o - 流式直传给浏览器
      （无磁盘、无队列、无任务面板，浏览器自带进度条）。
限制：单文件建议 ≤2GB；国内站点（B站/小红书等）因境外节点+风控不可用，主攻境外站点。
"""
import os
import re
import json
import subprocess
import sys

from flask import Flask, jsonify, request, Response, send_from_directory
import yt_dlp

app = Flask(__name__, static_folder=None)

PRICE = float(os.environ.get("PRICE", "9.9"))          # 预留（Vercel 版第一期无付费）
PRICE = round(PRICE, 2)

_FF = None


def ffmpeg_path():
    """imageio-ffmpeg 自带静态 ffmpeg（requirements 安装，无需系统安装）"""
    global _FF
    if _FF is None:
        try:
            import imageio_ffmpeg
            _FF = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _FF = ""
    return _FF


BILI_RE = re.compile(r"bilibili\.com|b23\.tv", re.I)
XHS_RE = re.compile(r"xiaohongshu\.com|xhslink\.com", re.I)
BLOCK_RE = re.compile(r"bilibili\.com|b23\.tv|xiaohongshu\.com|xhslink\.com|douyin\.com|kuaishou\.com", re.I)


def _scrub(text):
    t = str(text or "")
    return t[:400]


@app.after_request
def sec_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


def _num(x):
    try:
        return float(x) if x not in ("NA", "None", "", None) else None
    except (TypeError, ValueError):
        return None


def _friendly(text, lang="zh"):
    t = _scrub(text)
    if "Sign in to confirm" in t:
        return ("YouTube 需登录验证，请换一个视频试试",
                "YouTube requires sign-in verification, try another video")
    if "Private video" in t or "unavailable" in t.lower():
        return ("视频不存在或为私有", "Video unavailable or private")
    zh = "解析失败：网络异常或链接不受支持" if lang == "zh" else "Parse failed: network error or unsupported URL"
    return (zh + "\n" + t[-200:]) if t else zh


# ---------------- 解析 ----------------

@app.post("/api/parse")
def api_parse():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    lang = "en" if (request.headers.get("X-UI-Lang") or "").lower().startswith("en") else "zh"
    if not url:
        return jsonify(error=_e("请输入网址", "Please enter a URL", lang)), 400
    if BLOCK_RE.search(url):
        return jsonify(error=_e(
            "该国内站点在云部署版暂不支持（境外节点风控），请使用本地版",
            "Domestic sites are not supported in cloud edition", 400)), 400

    # proxy="" = 强制直连（绕开用户机器的系统代理，如 Clash）；需要代理时设环境变量 PROXY_URL
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "proxy": os.environ.get("PROXY_URL", "")}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception as ex:
        msg = _friendly(str(ex), lang)
        return jsonify(error=msg[0] if lang == "zh" else msg[1]), 502

    if info.get("_type") == "playlist":
        entries = [{"id": e.get("id") or "", "title": e.get("title") or "",
                    "url": e.get("url") or e.get("webpage_url") or e.get("id") or ""}
                   for e in (info.get("entries") or [])[:100] if e]
        return jsonify(type="playlist", title=info.get("title") or "Playlist",
                       count=len(entries), entries=entries)

    fmts = []
    for f in info.get("formats") or []:
        vc = f.get("vcodec") or "unknown"
        ac = f.get("acodec") or "unknown"
        if vc == "none" and ac == "none":
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        fmts.append({
            "format_id": str(f.get("format_id")),
            "ext": f.get("ext") or "?",
            "resolution": f.get("resolution") or (None if vc != "none" else "audio"),
            "height": f.get("height"), "fps": f.get("fps"),
            "vcodec": vc if vc != "none" else None,
            "acodec": ac if ac != "none" else None,
            "filesize": int(size) if size else None, "tbr": f.get("tbr"),
            "note": f.get("format_note") or "",
        })
    return jsonify(type="video", id=info.get("id", ""),
                   title=info.get("title") or "(untitled)",
                   uploader=info.get("uploader") or info.get("channel") or "",
                   duration=info.get("duration"),
                   extractor=info.get("extractor_key") or "",
                   webpage_url=info.get("webpage_url") or url,
                   thumbnail=info.get("thumbnail") or "",
                   formats=fmts, has_ffmpeg=bool(ffmpeg_path()),
                   cloud=True)


def _e(zh, en, lang):
    return en if lang == "en" else zh


# ---------------- 流式下载（serverless 核心：边下边传） ----------------

@app.get("/api/download")
def api_download():
    """GET 流式下载：浏览器直接另存（无需任务面板，进度由浏览器显示）"""
    url = (request.args.get("url") or "").strip()
    fmt = (request.args.get("format") or "b").strip()
    name = (request.args.get("name") or "video").strip()[:80]
    lang = "en" if (request.headers.get("X-UI-Lang") or "").lower().startswith("en") else "zh"
    if not url:
        return jsonify(error=_e("缺少网址", "Missing URL", lang)), 400
    if BLOCK_RE.search(url):
        return jsonify(error=_e("该国内站点在云部署版暂不支持", "Domestic sites unsupported", 400)), 400

    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    ff = ffmpeg_path()
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings",
           "-o", "-", "-f", fmt, "--no-playlist", "--no-part",
           "--proxy", os.environ.get("PROXY_URL", "")]
    if ff:
        cmd += ["--ffmpeg-location", ff]
    cmd.append(url)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as ex:
        return jsonify(error=_e(f"下载进程启动失败：{ex}", f"download spawn failed: {ex}", lang)), 502

    def gen():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    ext = "mp4" if fmt in ("b", "bv*+ba/b") or "+" in fmt else "m4a"
    return Response(stream_with_context_wrap(gen()),
                    mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"',
                             "Cache-Control": "no-store"})


def stream_with_context_wrap(gen):
    from flask import stream_with_context
    return stream_with_context(gen)


# ---------------- 前端静态 ----------------

@app.get("/")
def index():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "public"), "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(b"", mimetype="image/x-icon")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8768, threaded=True)
