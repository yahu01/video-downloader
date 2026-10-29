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

_COOKIES_FILE = None


def cookies_file():
    """COOKIES 环境变量（Netscape cookies.txt 内容）写 /tmp 临时文件，缓解 YouTube 数据中心 IP 风控"""
    global _COOKIES_FILE
    if _COOKIES_FILE is None:
        raw = os.environ.get("COOKIES", "").strip()
        if raw:
            import tempfile
            _COOKIES_FILE = os.path.join(tempfile.gettempdir(), "yt.cookies")
            with open(_COOKIES_FILE, "w", encoding="utf-8") as fh:
                fh.write(raw.replace(chr(92) + "n", chr(10)))
        else:
            _COOKIES_FILE = ""
    return _COOKIES_FILE
NLCH2 = chr(10)


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
    """统一返回 str（按语言选好），调用处不再做索引"""
    t = _scrub(text)
    if "Sign in to confirm" in t:
        return _e("YouTube 需登录验证，请换一个视频试试",
                  "YouTube requires sign-in verification, try another video", lang)
    if "Private video" in t or "unavailable" in t.lower():
        return _e("视频不存在或为私有", "Video unavailable or private", lang)
    base = _e("解析失败：网络异常或链接不受支持",
              "Parse failed: network error or unsupported URL", lang)
    return (base + NLCH2 + t[-200:]) if t else base

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
        return jsonify(error=_friendly(str(ex), lang)), 502

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

def _rq_proxy():
    """requests 转发时的代理（PROXY_URL 环境变量，默认直连）"""
    p = os.environ.get("PROXY_URL", "")
    return {"http": p, "https": p} if p else None


def _hdrs_for(meta, url):
    """按直链 URL 找到对应 format 的 http_headers（防盗链必需）"""
    for f in meta.get("formats") or []:
        if f.get("url") == url:
            return f.get("http_headers") or {}
    return {}


def _headers_str(h):
    """ffmpeg -headers 参数格式：Key: Value + CRLF 连接"""
    crlf = chr(13) + chr(10)
    return "".join(k + ": " + v + crlf for k, v in (h or {}).items())



def _pick_stream(meta, fmt):
    """从 dump-json 的 formats 里选流。
    返回 (url, headers, ext, kind)：kind=plain 单流直转 / merge 需 ffmpeg 合并"""
    fmts = [f for f in (meta.get("formats") or []) if f.get("url")]
    if not fmts:
        return None
    def key(f):
        return ((f.get("height") or 0), (f.get("tbr") or 0))
    if fmt == "ba":
        auds = [f for f in fmts if f.get("vcodec") in (None, "none")]
        if not auds:
            return None
        f = max(auds, key=lambda x: x.get("tbr") or 0)
        if (f.get("protocol") or "").startswith("m3u8"):
            return {"v": f["url"]}, f.get("http_headers") or {}, "m4a", "merge"
        return f["url"], f.get("http_headers") or {}, f.get("ext") or "m4a", "plain"
    if fmt == "b" or "+" in fmt:
        vids = [f for f in fmts if f.get("vcodec") not in (None, "none")]
        auds = [f for f in fmts if f.get("vcodec") in (None, "none")]
        prog = [f for f in vids if f.get("acodec") not in (None, "none")]
        if prog:                                   # 有带音频的合成格式（如 YouTube 18/22）
            f = max(prog, key=key)
            return f["url"], f.get("http_headers") or {}, f.get("ext") or "mp4", "plain"
        if vids and auds:                          # 分离流 → ffmpeg 管道合并
            v = max(vids, key=key)
            a = max(auds, key=lambda x: x.get("tbr") or 0)
            return {"v": v["url"], "a": a["url"]},                    v.get("http_headers") or {}, "mp4", "merge"
        if vids:                                   # 只有视频流
            f = max(vids, key=key)
            return f["url"], f.get("http_headers") or {}, f.get("ext") or "mp4", "plain"
        if auds:                                   # 纯音频站（如 SoundCloud）
            f = max(auds, key=lambda x: x.get("tbr") or 0)
            if (f.get("protocol") or "").startswith("m3u8"):
                return {"v": f["url"]}, f.get("http_headers") or {}, "m4a", "merge"
            return f["url"], f.get("http_headers") or {}, f.get("ext") or "m4a", "plain"
        return None
    f = next((f for f in fmts if str(f.get("format_id")) == fmt), None)
    if not f:
        return None
    return f["url"], f.get("http_headers") or {}, f.get("ext") or "mp4", "plain"


@app.get("/api/download")
def api_download():
    try:
        return _download_impl()
    except Exception:
        import traceback
        tb = traceback.format_exc()[-1500:]
        print("DOWNLOAD ERROR:", tb, flush=True)
        return jsonify(error="SERVER TRACEBACK: " + tb.replace(chr(10), " | ")), 500


def _download_impl():
    """GET 流式下载：dump-json 拿直链 → 服务端 requests/ffmpeg 拉流转发给浏览器。
    单流（progressive/音频）requests 转发；分离流（YouTube 高清）ffmpeg 管道实时合并。"""
    import requests as _rq
    url = (request.args.get("url") or "").strip()
    fmt = (request.args.get("format") or "b").strip()
    name = (request.args.get("name") or "video").strip()[:80]
    lang = "en" if (request.headers.get("X-UI-Lang") or "").lower().startswith("en") else "zh"
    if not url:
        return jsonify(error=_e("缺少网址", "Missing URL", lang)), 400
    if BLOCK_RE.search(url):
        return jsonify(error=_e("该国内站点在云部署版暂不支持", "Domestic sites unsupported", 400)), 400

    ff = ffmpeg_path()
    proxy = os.environ.get("PROXY_URL", "")
    base = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings",
            "--no-playlist", "--proxy", proxy]
    cf = cookies_file()
    if cf:
        base += ["--cookies", cf]

    # 第一步：dump-json 验证 + 元数据（直链在这里面）
    probe = base + ["--dump-json", "--no-playlist", url]
    try:
        pr = subprocess.run(probe, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return jsonify(error=_e("解析超时，请重试", "Parse timed out, retry", lang)), 504
    if pr.returncode != 0 or not pr.stdout.strip():
        detail = _scrub(pr.stderr[-200:]) if pr.stderr else "no output"
        return jsonify(error=_e("无法解析该链接：" + detail,
                                "Cannot parse this link: " + detail, lang)), 502
    try:
        meta = json.loads(pr.stdout.strip().splitlines()[-1])
    except ValueError:
        meta = {}
    title = meta.get("title") or name
    safe = re.sub(r'[\/:*?"<>|]', "_", title)[:80]

    pick = _pick_stream(meta, fmt)
    if not pick:
        return jsonify(error=_e("所选格式不可用", "Selected format unavailable", lang)), 400
    src, headers, ext, kind = pick
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36")

    if kind == "plain":
        def gen():
            with _rq.get(src, headers=headers, stream=True, timeout=(10, 300), proxies=_rq_proxy()) as up:
                up.raise_for_status()
                for chunk in up.iter_content(65536):
                    if chunk:
                        yield chunk
        ctype = "audio/mp4" if ext in ("m4a", "mp3") else "video/mp4"
        return Response(stream_with_context_wrap(gen()), mimetype=ctype,
                        headers={"Content-Disposition": "attachment; filename=\"" + safe + "." + ext + "\"",
                                 "Cache-Control": "no-store"})

    # merge：ffmpeg 管道实时合并（分离流，如 YouTube 高清）
    if not ff:
        return jsonify(error=_e("服务器缺少 ffmpeg，无法合并该画质",
                                "ffmpeg missing on server for this quality", lang)), 502
    vurl = src["v"] if isinstance(src, dict) else src
    aurl = src.get("a") if isinstance(src, dict) else None
    vh = dict(headers); vh.update(_hdrs_for(meta, vurl))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", vurl]
    if aurl:
        ah = dict(headers); ah.update(_hdrs_for(meta, aurl))
        cmd = [ff, "-hide_banner", "-loglevel", "error",
               "-headers", _headers_str(ah), "-i", aurl,
               "-headers", _headers_str(vh), "-i", vurl]
    else:
        cmd = [ff, "-hide_banner", "-loglevel", "error",
               "-headers", _headers_str(vh), "-i", vurl]
    if aurl:
        cmd += ["-map", "1:a", "-map", "0:v"] if False else []
    cmd += ["-c", "copy", "-movflags", "frag_keyframe+empty_moov+faststart",
            "-f", "mp4", "pipe:1"]
    try:
        penv = dict(os.environ)
        _px = os.environ.get("PROXY_URL", "")
        if _px:
            penv["http_proxy"] = penv["https_proxy"] = _px
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=penv)
    except Exception as ex:
        return jsonify(error=_e(f"合并进程启动失败：{ex}", f"merge spawn failed: {ex}", lang)), 502

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

    return Response(stream_with_context_wrap(gen()), mimetype="video/mp4",
                    headers={"Content-Disposition": "attachment; filename=\"" + safe + ".mp4\"",
                             "Cache-Control": "no-store"})

def stream_with_context_wrap(gen):
    from flask import stream_with_context
    return stream_with_context(gen)


# ---------------- 前端静态 ----------------

@app.errorhandler(404)
def _diag404(e):
    if request.path.startswith("/api/"):
        return jsonify(diag={"path": request.path, "script_root": request.script_root,
                             "method": request.method, "url_root": request.url_root}), 404
    return e


@app.get("/")
def index():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "public"), "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(b"", mimetype="image/x-icon")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8768, threaded=True)
