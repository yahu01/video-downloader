# -*- coding: utf-8 -*-
"""
万能视频下载站 · Vercel Serverless 版
架构：解析用 yt-dlp Python API（秒回）；下载用 yt-dlp -o - 流式直传给浏览器
      （无磁盘、无队列、无任务面板，浏览器自带进度条）。
限制：单文件建议 ≤2GB；国内站点（B站/小红书等）因境外节点+风控不可用，主攻境外站点。
"""
import urllib.request
import urllib.parse
import urllib.error
import os
import re
import json
import subprocess
import sys

from flask import Flask, jsonify, redirect, request, Response, send_from_directory, session
import yt_dlp

app = Flask(__name__, static_folder=None)

PRICE = float(os.environ.get("PRICE", "9.9"))          # 预留（Vercel 版第一期无付费）
PRICE = round(PRICE, 2)

_COOKIES_FILE = None
_DOUYIN_CF = None


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


# ---------------- 用户体系（GitHub 仓库当数据库） ----------------

DATA_REPO = os.environ.get("DATA_REPO", "yahu01/video-downloader-data")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
import hashlib as _hashlib
import base64 as _b64
import time as _time
import secrets as _secrets
from werkzeug.security import generate_password_hash, check_password_hash

app.secret_key = _hashlib.sha256(("vd-v1:" + GH_TOKEN).encode()).digest()

_DATA_CACHE = {"sha": None, "data": None, "ts": 0.0}
_DATA_LOCK = __import__("threading").Lock()


def _gh(path, method="GET", body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": "Bearer " + GH_TOKEN,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "video-downloader"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def data_read(force=False):
    """读 data.json（GitHub Contents API，10 秒实例缓存）"""
    with _DATA_LOCK:
        if not force and _DATA_CACHE["data"] is not None and _time.time() - _DATA_CACHE["ts"] < 10:
            return _DATA_CACHE["data"]
        r = _gh(f"/repos/{DATA_REPO}/contents/data.json")
        data = json.loads(_b64.b64decode(r["content"]).decode("utf-8"))
        _DATA_CACHE.update(sha=r["sha"], data=data, ts=_time.time())
        return data


def data_write(data):
    """写 data.json（带 sha 冲突重试）"""
    with _DATA_LOCK:
        for attempt in range(4):
            if _DATA_CACHE["sha"] is None:
                data_read(force=True)
            body = {"message": "update user data",
                    "content": _b64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode(),
                    "sha": _DATA_CACHE["sha"]}
            try:
                r = _gh(f"/repos/{DATA_REPO}/contents/data.json", method="PUT", body=body)
                _DATA_CACHE.update(sha=r["content"]["sha"], data=data, ts=_time.time())
                return
            except Exception:
                if attempt == 3:
                    raise
                data_read(force=True)


def data_config():
    return (data_read().get("config") or {})


def _find_user(name):
    return (data_read().get("users") or {}).get(name)


BILI_RE = re.compile(r"bilibili\.com|b23\.tv", re.I)
XHS_RE = re.compile(r"xiaohongshu\.com|xhslink\.com", re.I)
BLOCK_RE = re.compile(r"xiaohongshu\.com|xhslink\.com|kuaishou\.com", re.I)
DOUYIN_RE = re.compile(r"douyin\.com|iesdouyin\.com", re.I)   # 抖音/TikTok/B站 放行（云版支持抖音与TikTok；B站境外风控屏蔽）


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
    if "IP address is blocked" in t or "blocked from accessing" in t:
        return _e("该站点屏蔽了数据中心 IP（云部署无法解析），请换用其他站点或本地版",
                  "This site blocks datacenter IPs (cloud edition can't parse it)", lang)
    if "not available" in t.lower() and "tiktok" in t.lower():
        return _e("TikTok 限制服务器访问该视频", "TikTok restricts server access to this video", lang)
    base = _e("解析失败：网络异常或链接不受支持",
              "Parse failed: network error or unsupported URL", lang)
    return (base + NLCH2 + t[-200:]) if t else base

# ---------------- Auth / Users (cloud edition) ----------------

def _lang():
    return "en" if (request.headers.get("X-UI-Lang") or "").lower().startswith("en") else "zh"


def _cfg_get(key, default):
    return data_config().get(key, default)


def _users():
    return data_read().get("users") or {}


def _save_user(name, profile):
    d = data_read()
    d.setdefault("users", {})[name] = profile
    data_write(d)


@app.post("/api/auth/register")
def auth_register():
    d = request.get_json(silent=True) or {}
    name = (d.get("username") or "").strip()
    pwd = d.get("password") or ""
    if not (3 <= len(name) <= 20):
        return jsonify(error=_e("Username must be 3-20 characters", "Username must be 3-20 characters", _lang())), 400
    if len(pwd) < 6:
        return jsonify(error=_e("Password must be at least 6 characters", "Password must be at least 6 characters", _lang())), 400
    if name in _users():
        return jsonify(error=_e("Username already taken", "Username already taken", _lang())), 409
    _save_user(name, {"pass_hash": generate_password_hash(pwd), "paid": False,
                      "created": int(_time.time()), "provider": "local"})
    session["uid"] = name
    return jsonify(ok=True, user={"name": name, "paid": False})


@app.post("/api/auth/login")
def auth_login():
    d = request.get_json(silent=True) or {}
    name = (d.get("username") or "").strip()
    pwd = d.get("password") or ""
    prof = _find_user(name)
    if not prof or not check_password_hash(prof.get("pass_hash", ""), pwd):
        return jsonify(error=_e("Wrong username or password", "Wrong username or password", _lang())), 401
    session["uid"] = name
    return jsonify(ok=True, user={"name": name, "paid": bool(prof.get("paid"))})


@app.post("/api/auth/logout")
def auth_logout():
    session.pop("uid", None)
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    uid = session.get("uid")
    prof = _find_user(uid) if uid else None
    if uid and not prof:
        session.pop("uid", None)
        uid = None
    providers = []
    if (data_config().get("github") or {}).get("client_id"):
        providers.append("github")
    if (data_config().get("google") or {}).get("client_id"):
        providers.append("google")
    return jsonify(
        user=({"name": uid, "paid": bool(prof.get("paid"))} if prof else None),
        paid_required=bool(_cfg_get("paid_required", False)),
        oauth=providers)


@app.get("/oauth/google/login")
def google_oauth_login():
    cfg = data_config().get("google") or {}
    cid = cfg.get("client_id") or ""
    if not cid:
        return jsonify(error="Google login is not configured yet"), 501
    state = _secrets.token_hex(16)
    session["g_state"] = state
    host = request.host_url.rstrip("/")
    q = urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": host + "/oauth/google/callback",
        "response_type": "code", "scope": "openid email profile", "state": state,
    })
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + q)


@app.route("/oauth/google/callback")
def google_oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state", "")
    cfg = data_config().get("google") or {}
    if not code or state != session.get("g_state", "") or not cfg.get("client_id"):
        return redirect("/?oauth=err")
    session.pop("g_state", None)
    try:
        body = urllib.parse.urlencode({
            "client_id": cfg["client_id"], "client_secret": cfg.get("client_secret", ""),
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": request.host_url.rstrip("/") + "/oauth/google/callback",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token",
                                     data=body, method="POST")
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
        id_token = tok.get("id_token") or ""
        if not id_token:
            return redirect("/?oauth=err")
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        info = json.loads(_b64.urlsafe_b64decode(payload))
        email = info.get("email") or ""
        if not email:
            return redirect("/?oauth=err")
        if not _find_user(email):
            _save_user(email, {"pass_hash": generate_password_hash(_secrets.token_hex(16)),
                               "paid": False, "created": int(_time.time()),
                               "provider": "google", "email": email})
        session["uid"] = email
        return redirect("/?oauth=ok")
    except Exception:
        return redirect("/?oauth=err")


@app.get("/oauth/github/login")
def gh_oauth_login():
    try:
        return _gh_login_impl()
    except Exception:
        import traceback
        tb = traceback.format_exc()[-800:]
        print("GH LOGIN ERROR:", tb, flush=True)
        return jsonify(error="GH LOGIN TRACEBACK: " + tb.replace(chr(10), " | ")), 500


def _gh_login_impl():
    cfg = data_config().get("github") or {}
    cid = cfg.get("client_id") or ""
    if not cid:
        return jsonify(error="GitHub login is not configured yet"), 501
    state = _secrets.token_hex(16)
    session["gh_state"] = state
    host = request.host_url.rstrip("/")
    q = urllib.parse.urlencode({"client_id": cid, "redirect_uri": host + "/oauth/github/callback",
                        "scope": "read:user user:email", "state": state})
    return redirect("https://github.com/login/oauth/authorize?" + q)


@app.route("/oauth/github/callback")
def gh_oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state", "")
    cfg = data_config().get("github") or {}
    if not code or state != session.get("gh_state", "") or not cfg.get("client_id"):
        return redirect("/?oauth=err")
    session.pop("gh_state", None)
    try:
        body = urllib.parse.urlencode({"client_id": cfg["client_id"],
                               "client_secret": cfg.get("client_secret", ""),
                               "code": code}).encode()
        req = urllib.request.Request("https://github.com/login/oauth/access_token",
                                     data=body, method="POST",
                                     headers={"Accept": "application/json",
                                              "User-Agent": "video-downloader"})
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("access_token") or ""
        if not tok:
            return redirect("/?oauth=err")
        h = {"Authorization": "Bearer " + tok, "Accept": "application/json",
             "User-Agent": "video-downloader"}
        uinfo = json.loads(urllib.request.urlopen(
            urllib.request.Request("https://api.github.com/user", headers=h), timeout=30).read())
        email = uinfo.get("email") or ""
        if not email:
            emails = json.loads(urllib.request.urlopen(
                urllib.request.Request("https://api.github.com/user/emails", headers=h),
                timeout=30).read())
            email = next((x["email"] for x in emails if x.get("primary")), "")
        email = email or (uinfo.get("login") or "")
        if not email:
            return redirect("/?oauth=err")
        if not _find_user(email):
            _save_user(email, {"pass_hash": generate_password_hash(_secrets.token_hex(16)),
                               "paid": False, "created": int(_time.time()),
                               "provider": "github", "email": email})
        session["uid"] = email
        return redirect("/?oauth=ok")
    except Exception:
        return redirect("/?oauth=err")


# ---------------- GitHub one-click setup (App Manifest) ----------------

@app.get("/oauth/github/setup")
def gh_setup():
    host = request.host_url.rstrip("/")
    manifest = {
        "name": "Video Downloader Login",
        "url": host + "/",
        "hook_attributes": {"url": host + "/oauth/github/hook", "active": False},
        "redirect_url": host + "/oauth/github/setup/callback",
        "callback_urls": sorted({host + "/oauth/github/callback"}),
        "setup_url": host + "/",
        "public": True,
        "request_oauth_user_on_install": True,
        "default_permissions": {},
    }
    mjson = json.dumps(manifest, separators=(",", ":"))
    html = (
        '<!doctype html><meta charset="utf-8"><title>Redirecting to GitHub...</title>'
        '<body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;'
        'display:grid;place-items:center;height:100vh"><div style="text-align:center">'
        '<p style="font-size:16px">Redirecting to GitHub...</p>'
        '<p style="color:#8b949e;font-size:13px">Click the green "Create GitHub App" button, '
        'you will be returned here automatically.</p></div>'
        '<form id="f" method="post" action="https://github.com/settings/apps/new">'
        '<input type="hidden" name="manifest" value="'
        + _b64.b64encode(mjson.encode()).decode() + '"></form>'
        '<script>document.getElementById("f").submit()</script></body>')
    return Response(html, mimetype="text/html")


@app.route("/oauth/github/setup/callback")
def gh_setup_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?oauth=err")
    req = urllib.request.Request(
        "https://api.github.com/app-manifests/" + code + "/conversions",
        data=b"{}", method="POST",
        headers={"Accept": "application/json", "User-Agent": "video-downloader"})
    try:
        appinfo = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception:
        return redirect("/?oauth=err")
    cid = appinfo.get("client_id") or ""
    csec = appinfo.get("client_secret") or ""
    if not (cid and csec):
        return redirect("/?oauth=err")
    d = data_read()
    c = d.setdefault("config", {})
    gh = c.get("github") if isinstance(c.get("github"), dict) else {}
    gh = gh or {}
    gh.update({"client_id": cid, "client_secret": csec})
    c["github"] = gh
    data_write(d)
    return redirect("/?oauth=ghsetup")


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
    if DOUYIN_RE.search(url):
        cf = douyin_cookie_file()
        if cf:
            opts["cookiefile"] = cf
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



def douyin_cookie_file():
    """生成 douyin 游客 cookie 文件（访问主页拿 __ac_nonce 等 set-cookie）。
    返回文件路径；失败返回 None。结果缓存到实例级 _DOUYIN_CF。"""
    global _DOUYIN_CF
    if _DOUYIN_CF is not None:
        return _DOUYIN_CF or None
    try:
        import requests as _rq
        sess = _rq.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"})
        sess.get("https://www.douyin.com/", timeout=10)
        jars = sess.cookies
        if not jars:
            _DOUYIN_CF = ""
            return None
        cf = os.path.join(tempfile.gettempdir(), "douyin.cookies")
        nl = chr(10)
        with open(cf, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File" + nl)
            exp = str(int(time.time()) + 86400)
            for c in jars:
                fh.write("\t".join([c.domain, "TRUE", c.path,
                          "FALSE" if not c.secure else "TRUE", exp, c.name, c.value or ""]) + nl)
        _DOUYIN_CF = cf
        return cf
    except Exception:
        _DOUYIN_CF = ""
        return None


def await_douyin_cookies(base, url):
    cf = douyin_cookie_file()
    return base + ["--cookies", cf] if cf else base


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
    if DOUYIN_RE.search(url):
        base = await_douyin_cookies(base, url)
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
