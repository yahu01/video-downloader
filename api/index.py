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
    ad = data_config().get("ad") or {}
    if not ad.get("enabled"):
        ad = {}
    return jsonify(
        user=({"name": uid, "paid": bool(prof.get("paid"))} if prof else None),
        paid_required=bool(_cfg_get("paid_required", False)),
        price=_cfg_get("price", 9.9),
        oauth=providers,
        ad=ad)


def _rq_get_img(url):
    import requests as _rq
    r = _rq.get(url, timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
                         "Referer": "https://www.douyin.com/"})
    r.raise_for_status()
    return r.content


def _tikwm_resolve(url):
    """tikwm.com 第三方解析：TikTok + Douyin（抖音）无水印直链。
    优先走 Cloudflare Worker 中转（机房 IP 被 tikwm 风控）；失败再直连重试 + 镜像域名。"""
    import time as _t2

    # 1) Cloudflare Worker 中转（CF 出口不受 tikwm 机房 IP 风控）
    try:
        req = urllib.request.Request(
            "https://vd-resolver.jiangpack2026.workers.dev/resolve?k=vd-resolver-2026&u="
            + urllib.parse.quote(url, safe=""),
            headers={"User-Agent": "video-downloader", "Accept": "application/json"})
        r0 = json.loads(urllib.request.urlopen(req, timeout=35).read())
        if r0.get("code") == 0 and r0.get("data"):
            r = r0
            d = r["data"]
            direct = {}
            if d.get("hdplay"):
                direct["hd"] = d["hdplay"]
            if d.get("play"):
                direct.setdefault("hd", d["play"])
                direct["sd"] = d["play"]
            if d.get("wmplay"):
                direct["wm"] = d["wmplay"]
            if d.get("music"):
                direct["mp3"] = d["music"]
            images = [img.get("url") for img in (d.get("images") or []) if img.get("url")]
            if not direct and images:
                return {"title": d.get("title") or "", "direct": {}, "images": images,
                        "thumbnail": images[0], "duration": d.get("duration")}
            if not direct:
                return None
            return {"title": d.get("title") or "", "direct": direct, "images": images,
                    "thumbnail": d.get("origin_cover") or d.get("cover") or "",
                    "duration": d.get("duration")}
    except Exception:
        pass

    # 2) 直连 tikwm（重试 3 次 + 镜像域名）
    hosts = ["https://www.tikwm.com", "https://tikwm.com", "https://tikwm.org"]
    r = None
    for attempt in range(3):
        host = hosts[attempt % len(hosts)]
        try:
            req = urllib.request.Request(
                host + "/api/?url=" + urllib.parse.quote(url, safe="") + "&hd=1",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
                         "Referer": host + "/",
                         "Accept": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=20).read())
            if r.get("code") == 0 and r.get("data"):
                break
            # code!=0（限流/风控）→ 换下一个域名重试
            r = None
        except Exception:
            r = None
        _t2.sleep(0.8)
    if not r or not r.get("data"):
        return None
    try:
        d = r["data"]
        direct = {}
        if d.get("hdplay"):
            direct["hd"] = d["hdplay"]
        if d.get("play"):
            direct.setdefault("hd", d["play"])
            direct["sd"] = d["play"]
        if d.get("wmplay"):
            direct["wm"] = d["wmplay"]
        if d.get("music"):
            direct["mp3"] = d["music"]
        images = [img.get("url") for img in (d.get("images") or []) if img.get("url")]
        if not direct and images:
            return {"title": d.get("title") or "", "direct": {}, "images": images,
                    "thumbnail": images[0], "duration": d.get("duration")}
        if not direct:
            return None
        return {"title": d.get("title") or "", "direct": direct,
                "images": images,
                "thumbnail": d.get("origin_cover") or d.get("cover") or "",
                "duration": d.get("duration")}
    except Exception:
        return None


def _fmt_rate_size(bps, size):
    """码率/体积人类可读：2000000bps + 13107200B → "2M · 12.5MB"""
    def rb(x):
        if x >= 1e9:
            return f"{x/1e9:.1f}".rstrip("0").rstrip(".") + "Gbps"
        if x >= 1e6:
            return f"{x/1e6:.1f}".rstrip("0").rstrip(".") + "M"
        return f"{x/1e3:.0f}K"
    out = rb(bps)
    if size:
        mb = size / 1048576
        out += " · " + (f"{mb/1024:.2f}G" if mb >= 1024 else f"{mb:.1f}MB")
    return out


def _cd_header(safe, ext):
    """RFC 5987 双文件名头：HTTP 头只允许 latin-1，非 ASCII（日文/中文标题）必须走
    filename* 扩展，否则 WSGI 抛 UnicodeEncodeError → 函数 500。"""
    from urllib.parse import quote as _uq
    ascii_name = safe.encode("ascii", "ignore").decode().strip().strip(".") or "video"
    ascii_name = re.sub(r'[\\/:*?"<>|#]+', "_", ascii_name)[:60]
    utf8_name = re.sub(r'[\\/:*?"<>|#]+', "_", str(safe))[:60] or "video"
    return ('attachment; filename="' + ascii_name + "." + ext + '"; '
            "filename*=UTF-8''" + _uq(utf8_name) + "." + ext)


def _upstream_headers(u):
    """按直链域名构造防盗链请求头：抖音/TikTok CDN 必须 Referer，且要求 Range 请求。"""
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0"}
    if "douyin" in host or "iesdouyin" in host or "douyinvod" in host or "zjcdn" in host:
        h["Referer"] = "https://www.douyin.com/"
    elif "tikwm" in host:
        h["Referer"] = "https://www.tikwm.com/"
    elif "tiktok" in host or "akamaized" in host or "byteoversea" in host or "muscdn" in host or "musical" in host:
        h["Referer"] = "https://www.tiktok.com/"
    elif "bilibili" in host or "hdslb" in host:
        h["Referer"] = "https://www.bilibili.com/"
    h["Range"] = "bytes=0-"
    return h


@app.get("/api/stream")
def api_stream():
    """第三方直链的代理流：服务端拉流转发给浏览器（带下载文件名）"""
    u = (request.args.get("u") or "").strip()
    name = (request.args.get("name") or "video").strip()[:80]
    ext = (request.args.get("ext") or "mp4").strip()[:6]
    if not u.startswith(("http://", "https://")):
        return jsonify(error="invalid url"), 400
    host = urllib.parse.urlparse(u).hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254") or \
       host.endswith(".internal") or re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", host):
        return jsonify(error="blocked"), 400
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    import requests as _rq
    try:
        up = _rq.get(u, stream=True, timeout=(15, 300), headers=_upstream_headers(u))
        up.raise_for_status()
    except Exception as ex:
        return jsonify(error=f"fetch failed: {str(ex)[:120]}"), 502
    ctype = up.headers.get("Content-Type") or ("audio/mpeg" if ext == "mp3" else "video/mp4")
    if "html" in ctype.lower():
        ctype = "video/mp4" if ext != "mp3" else "audio/mpeg"
    def gen():
        try:
            for chunk in up.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            up.close()
    return Response(stream_with_context_wrap(gen()), mimetype=ctype,
                    headers={"Content-Disposition": _cd_header(safe, ext),
                             "Cache-Control": "no-store"})


@app.get("/api/zip")
def api_zip():
    """图集打包：多图服务端拉取 → 内存 ZIP → 浏览器下载"""
    import io
    import zipfile as _zipmod
    import requests as _rq
    urls = [u for u in request.args.getlist("u") if u.startswith(("http://", "https://"))]
    name = re.sub(r'[\/:*?"<>|]', "_", (request.args.get("name") or "gallery").strip())[:70]
    if not urls:
        return jsonify(error="no urls"), 400
    if len(urls) > 60:
        return jsonify(error="too many images"), 400
    buf = io.BytesIO()
    ok_cnt = 0
    with _zipmod.ZipFile(buf, "w", _zipmod.ZIP_DEFLATED) as zf:
        for i, u in enumerate(urls):
            try:
                r = _rq.get(u, timeout=(15, 60),
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
                                     "Referer": "https://www.douyin.com/"})
                r.raise_for_status()
                ext = ".jpg"
                ct = (r.headers.get("Content-Type") or "").lower()
                if "png" in ct: ext = ".png"
                elif "webp" in ct: ext = ".webp"
                zf.writestr(f"{i+1:02d}{ext}", r.content)
                ok_cnt += 1
            except Exception:
                continue
    if ok_cnt == 0:
        return jsonify(error="all images failed"), 502
    buf.seek(0)
    return Response(buf, mimetype="application/zip",
                    headers={"Content-Disposition": _cd_header(name, "zip"),
                             "Cache-Control": "no-store"})


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
            "小红书/快手暂不支持云端解析（平台强制登录+签名风控），已支持：抖音/TikTok/YouTube/Twitter 等",
            "Xiaohongshu/Kuaishou are not supported in cloud edition", 400)), 400

    # 抖音/TikTok 图集（images only）→ 服务端打包 ZIP 直接下载
    if (DOUYIN_RE.search(url) or re.search(r"tiktok\.com", url, re.I)) and request.args.get("zip") == "1":
        tk = _tikwm_resolve(url) or {}
        imgs = tk.get("images") or []
        if imgs:
            import io
            import zipfile as _zipmod
            buf = io.BytesIO()
            with _zipmod.ZipFile(buf, "w", _zipmod.ZIP_DEFLATED) as zf:
                for i, iu in enumerate(imgs):
                    try:
                        rr = _rq_get_img(iu)
                        zf.writestr(f"{i+1:02d}.jpg", rr)
                    except Exception:
                        continue
            buf.seek(0)
            safe = re.sub(r'[\\/:*?"<>|]', "_", url)[:60]
            return Response(buf, mimetype="application/zip",
                            headers={"Content-Disposition": _cd_header(safe + "-images", "zip")})

    # 抖音 / TikTok：优先 tikwm 第三方解析（无水印直链，绕开双平台风控）
    if DOUYIN_RE.search(url) or re.search(r"tiktok\.com", url, re.I):
        tk = _tikwm_resolve(url)
        if tk and tk.get("images") and not tk.get("direct"):
            return jsonify(type="gallery", thirdparty=True, id="",
                           title=tk.get("title") or "(图集)",
                           uploader="", duration=None,
                           extractor="抖音/TikTok 图集", webpage_url=url,
                           thumbnail=tk["images"][0], images=tk["images"],
                           formats=[], has_ffmpeg=False)
        if tk:
            direct = tk["direct"]
            fmts = []
            if direct.get("hd"):
                fmts.append({"format_id": "hd", "ext": "mp4", "label": "无水印高清"})
            if direct.get("sd") and direct.get("sd") != direct.get("hd"):
                fmts.append({"format_id": "sd", "ext": "mp4", "label": "无水印标清"})
            if direct.get("wm"):
                fmts.append({"format_id": "wm", "ext": "mp4", "label": "带水印"})
            if direct.get("mp3"):
                fmts.append({"format_id": "mp3", "ext": "mp3", "label": "音频 MP3"})
            return jsonify(type="video", thirdparty=True, id="",
                           title=tk["title"] or "(video)",
                           uploader="", duration=tk.get("duration"),
                           extractor="TikTok/抖音", webpage_url=url,
                           thumbnail=tk.get("thumbnail") or "",
                           formats=fmts, direct=direct, has_ffmpeg=False)
        # tikwm 失败（抖音场景常见）→ 回退公共 hybrid API 实例
        try:
            import requests as _rq2
            sess2 = _rq2.Session()
            sess2.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36"})
            res2 = sess2.get("https://lvhomeproxy2.dpdns.org/api/hybrid/video_data",
                             params={"url": url, "minimal": False}, timeout=40)
            d2 = res2.json()
            if d2.get("code") == 2000 and d2.get("data"):
                dd = d2["data"]
                v2 = dd.get("video") or {}
                brs = v2.get("bit_rate") or []
                brs.sort(key=lambda x: x.get("bit_rate") or 0, reverse=True)
                fmts2 = []
                for b in brs[:6]:
                    g = b.get("play_addr") or {}
                    u2 = (g.get("url_list") or [""])[0]
                    if u2:
                        kbps = b.get("bit_rate") or 0
                        fmts2.append({"format_id": "bit" + str(b.get("bit_rate")),
                                      "ext": "mp4", "url": u2,
                                      "label": _fmt_rate_size(kbps, b.get("data_size")),
                                      "filesize": b.get("data_size")})
                mu = ""
                try:
                    mu = ((dd.get("music") or {}).get("play_url") or {}).get("url_list", [""])[0]
                except Exception:
                    pass
                if mu:
                    fmts2.append({"format_id": "mp3", "ext": "mp3", "url": mu,
                                  "label": "音频 MP3", "filesize": None})
                cover = ""
                try:
                    cover = str(((dd.get("video") or {}).get("cover") or {}).get("url_list", [""])[0])
                except Exception:
                    pass
                if fmts2:
                    return jsonify(type="video", thirdparty=True, id=dd.get("aweme_id") or "",
                                   title=dd.get("desc") or "(video)",
                                   uploader=str((dd.get("author") or {}).get("nickname") or ""),
                                   duration=int(v2.get("duration") or 0) // 1000,
                                   extractor="抖音", webpage_url=url,
                                   thumbnail=cover, formats=fmts2,
                                   direct={"play": ((v2.get("play_addr") or {}).get("url_list") or [""])[0]},
                                   has_ffmpeg=False)
                # 无视频档位 → 尝试图集（data.images）
                imgs = [i.get("url") for i in (dd.get("images") or []) if i.get("url")]
                if imgs:
                    return jsonify(type="gallery", thirdparty=True, id=dd.get("aweme_id") or "",
                                   title=dd.get("desc") or "(图集)",
                                   uploader=str((dd.get("author") or {}).get("nickname") or ""),
                                   duration=None,
                                   extractor="抖音图集", webpage_url=url,
                                   thumbnail=imgs[0], images=imgs,
                                   formats=[], has_ffmpeg=False)
        except Exception:
            pass

    # proxy="" = 强制直连（绕开用户机器的系统代理，如 Clash）；需要代理时设环境变量 PROXY_URL
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "proxy": os.environ.get("PROXY_URL", "")}
    if DOUYIN_RE.search(url):
        cf = douyin_cookie_file()
        if cf:
            opts["cookiefile"] = cf
    if re.search(r"youtube\.com|youtu\.be", url, re.I):
        cf = cookies_file()   # YouTube 登录态（COOKIES 环境变量），过人机验证墙
        if cf:
            opts["cookiefile"] = cf
            opts["extractor_args"] = {"youtube": {"player_client": ["tv,web_safari,web"]}}
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

    # 抖音/TikTok：第三方全失败且 yt-dlp 只拿到纯音频（平台对机房 IP 的风控特征）
    # → 明确报错，避免渲染出没有视频可下载的坏卡片
    if (DOUYIN_RE.search(url) or re.search(r"tiktok\.com", url, re.I)) and \
       not any((f.get("vcodec") or "none") not in ("none", "", None) for f in (info.get("formats") or [])):
        return jsonify(error=_e(
            "解析源暂时不可用（平台风控/限流），请稍后重试或换个链接",
            "Parsing source temporarily unavailable (rate-limited), please retry later", lang)), 502

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



# ---------------- Admin (cloud edition) ----------------

import time as _adm_time

_ADMIN_FAILS = {}


def _admin_password():
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if pw:
        return pw
    return str(data_config().get("admin_pass") or "")


def _admin_user():
    return str(os.environ.get("ADMIN_USER", "")
               or data_config().get("admin_user") or "admin")


def _verify_admin_pw(pw):
    """环境变量优先；其次后台保存的哈希；兼容旧明文 admin_pass。"""
    env = os.environ.get("ADMIN_PASSWORD", "")
    if env:
        return pw == env
    c = data_config()
    h = c.get("admin_pass_hash")
    if h:
        try:
            return check_password_hash(str(h), pw)
        except Exception:
            return False
    stored = c.get("admin_pass")
    return bool(stored) and pw == str(stored)


# ---- 图形验证码（无状态：SVG 服务端渲染，答案仅以 HMAC 形式随 token 下发） ----
import hmac as _hmac
import random as _rnd

_CAP_TTL = 300  # 5 分钟


def _cap_hmac(text):
    return _hmac.new(app.secret_key, text.encode(), _hashlib.sha256).hexdigest()[:20]


def _captcha_svg(text):
    w, h = 150, 42
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<rect width="{w}" height="{h}" fill="#eef1f5" rx="6"/>']
    cols = ["#b85c5c", "#5c8ab8", "#6aa84f", "#8a6ab8", "#c78a3d"]
    for _ in range(5):
        x1, y1, x2, y2 = _rnd.randint(0, w), _rnd.randint(0, h), _rnd.randint(0, w), _rnd.randint(0, h)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{_rnd.choice(cols)}" stroke-width="1.5" opacity="0.55"/>')
    for _ in range(8):
        parts.append(f'<circle cx="{_rnd.randint(0, w)}" cy="{_rnd.randint(0, h)}" '
                     f'r="{_rnd.randint(1, 2)}" fill="{_rnd.choice(cols)}" opacity="0.5"/>')
    x = 14
    for ch in text:
        if ch == " ":
            x += 5
            continue
        rot = _rnd.randint(-16, 16)
        y = 28 + _rnd.randint(-3, 3)
        fs = 21 + _rnd.randint(0, 4)
        parts.append(f'<text x="{x}" y="{y}" font-family="Georgia, serif" font-size="{fs}" '
                     f'fill="#1a1a2e" transform="rotate({rot} {x} {y})">{ch}</text>')
        x += fs - 5
    parts.append("</svg>")
    return "".join(parts)


def _captcha_check(cid, user_ans):
    try:
        blob, sig = str(cid).split(".", 1)
        if not _hmac.compare_digest(sig, _cap_hmac(blob)):
            return False
        pad = "=" * (-len(blob) % 4)
        p = json.loads(_b64.urlsafe_b64decode(blob + pad).decode())
        exp = int(p.get("exp") or 0)
        if _time.time() > exp:
            return False
        uh = _cap_hmac(f"{str(user_ans).strip()}|{exp}")
        return _hmac.compare_digest(uh, str(p.get("h")))
    except Exception:
        return False


def _admin_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def _admin_limited():
    rec = _ADMIN_FAILS.get(_admin_ip())
    return bool(rec and rec[0] >= 8 and _adm_time.time() - rec[1] < 900)


def _admin_fail():
    key = _admin_ip()
    c, t0 = _ADMIN_FAILS.get(key, [0, _adm_time.time()])
    if _adm_time.time() - t0 > 900:
        c, t0 = 0, _adm_time.time()
    _ADMIN_FAILS[key] = [c + 1, t0]


def _admin_reset():
    _ADMIN_FAILS.pop(_admin_ip(), None)


def _admin_ok():
    return session.get("admin") is True


def _admin_gate():
    if not _admin_ok():
        return jsonify(error="admin auth required"), 401
    return None


@app.get("/api/admin/captcha")
def admin_captcha():
    a, b = _rnd.randint(10, 89), _rnd.randint(2, 9)
    if _rnd.random() < 0.5:
        q, ans = f"{a} + {b}", a + b
    else:
        if b > a:
            a, b = b, a
        q, ans = f"{a} - {b}", a - b
    exp = int(_time.time()) + _CAP_TTL
    p = {"h": _cap_hmac(f"{ans}|{exp}"), "exp": exp}
    blob = _b64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
    return jsonify(id=blob + "." + _cap_hmac(blob), svg=_captcha_svg(q + " = ?"))


@app.post("/api/admin/login")
def admin_login():
    if _admin_limited():
        return jsonify(error="too many attempts"), 429
    d = request.get_json(silent=True) or {}
    if not _captcha_check(str(d.get("captcha_id") or ""), str(d.get("captcha") or "")):
        return jsonify(error="captcha invalid"), 400
    uname = str(d.get("username") or "").strip()
    pw = str(d.get("password") or "")
    if uname != _admin_user() or not pw or not _verify_admin_pw(pw):
        _admin_fail()
        return jsonify(error="wrong username or password"), 401
    _admin_reset()
    session["admin"] = True
    return jsonify(ok=True)


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return jsonify(ok=True)


@app.get("/api/admin/session")
def admin_session():
    return jsonify(admin=_admin_ok())


@app.get("/api/admin/data")
def admin_data():
    gate = _admin_gate()
    if gate:
        return gate
    users = data_read().get("users") or {}
    out = []
    for name, p in users.items():
        p = p or {}
        out.append({"name": name, "provider": p.get("provider") or "local",
                    "paid": bool(p.get("paid")), "created": p.get("created"),
                    "email": p.get("email") or ""})
    out.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return jsonify(users=out,
                   config={"paid_required": bool(data_config().get("paid_required", False)),
                           "price": float(data_config().get("price", PRICE))})


def _yt_cookie_status():
    """解析 COOKIES 环境变量里的 Netscape cookie，报告 YouTube 会话有效期"""
    raw = os.environ.get("COOKIES", "").strip()
    if not raw:
        return {"configured": False, "days_left": None, "expires": None}
    exp = 0
    for line in raw.splitlines():
        p = line.split("\t")
        if len(p) >= 5 and p[4].strip().isdigit():
            exp = max(exp, int(p[4]))
    if not exp:
        return {"configured": True, "days_left": None, "expires": None}
    days = int((exp - _time.time()) // 86400)
    return {"configured": True, "days_left": days,
            "expires": _time.strftime("%Y-%m-%d", _time.localtime(exp))}


@app.get("/api/admin/stats")
def admin_stats():
    gate = _admin_gate()
    if gate:
        return gate
    users = data_read().get("users") or {}
    day0 = _time.time() - _time.time() % 86400
    total = len(users)
    paid = sum(1 for u in users.values() if (u or {}).get("paid"))
    gh = sum(1 for u in users.values() if (u or {}).get("provider") == "github")
    local = sum(1 for u in users.values() if (u or {}).get("provider") == "local")
    today = sum(1 for u in users.values() if ((u or {}).get("created") or 0) >= day0)
    return jsonify(total=total, paid=paid, github=gh, local=local, today=today,
                   cookie=_yt_cookie_status())


@app.post("/api/admin/user/paid")
def admin_user_paid():
    gate = _admin_gate()
    if gate:
        return gate
    d = request.get_json(silent=True) or {}
    name = d.get("name") or ""
    dd = data_read()
    users = dd.get("users") or {}
    if name not in users:
        return jsonify(error="user not found"), 404
    users[name]["paid"] = bool(d.get("paid"))
    data_write(dd)
    return jsonify(ok=True)


@app.post("/api/admin/user/delete")
def admin_user_delete():
    gate = _admin_gate()
    if gate:
        return gate
    d = request.get_json(silent=True) or {}
    name = d.get("name") or ""
    dd = data_read()
    if name not in (dd.get("users") or {}):
        return jsonify(error="user not found"), 404
    del dd["users"][name]
    data_write(dd)
    return jsonify(ok=True)


@app.get("/api/admin/config")
def admin_config_get():
    gate = _admin_gate()
    if gate:
        return gate
    c = data_read().get("config") or {}
    return jsonify(paid_required=bool(c.get("paid_required")),
                   price=c.get("price", 9.9),
                   admin_user=_admin_user(),
                   ad=c.get("ad") or {})


@app.post("/api/admin/config")
def admin_config_set():
    gate = _admin_gate()
    if gate:
        return gate
    d = request.get_json(silent=True) or {}
    dd = data_read()
    c = dd.setdefault("config", {})
    if "paid_required" in d:
        c["paid_required"] = bool(d.get("paid_required"))
    if "price" in d:
        try:
            c["price"] = round(float(d["price"]), 2)
        except (TypeError, ValueError):
            pass
    if d.get("admin_user"):
        c["admin_user"] = str(d["admin_user"]).strip()
    if d.get("admin_pass"):
        c["admin_pass_hash"] = generate_password_hash(str(d["admin_pass"]))
    if "ad" in d:
        a = d.get("ad") or {}
        c["ad"] = {
            "enabled": bool(a.get("enabled")),
            "title": str(a.get("title") or "")[:60],
            "text": str(a.get("text") or "")[:200],
            "url": str(a.get("url") or "")[:300],
            "image": str(a.get("image") or "")[:300],
            "button": str(a.get("button") or "")[:20],
        }
    data_write(dd)
    return jsonify(ok=True)


@app.get("/admin")
def admin_page():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "..", "public"), "admin.html")


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
            exp = str(int(_time.time()) + 86400)
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
    if re.search(r"youtube\.com|youtu\.be", url, re.I):
        cf = cookies_file()
        if cf:
            base += ["--cookies", cf]
    probe = base + ["--dump-json", "--no-playlist", url]
    try:
        pr = subprocess.run(probe, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return jsonify(error=_e("解析超时，请重试", "Parse timed out, retry", lang)), 504
    if pr.returncode != 0 or not pr.stdout.strip():
        # 受限视频（仅故事板等）：忽略无格式错误重试拿元数据
        pr2 = subprocess.run(probe + ["--ignore-no-formats-error"],
                             capture_output=True, text=True, timeout=120)
        if pr2.returncode == 0 and pr2.stdout.strip():
            pr = pr2
        else:
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
                        headers={"Content-Disposition": _cd_header(safe, ext),
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
                    headers={"Content-Disposition": _cd_header(safe, "mp4"),
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
