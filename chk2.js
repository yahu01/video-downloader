
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const I18N = {
zh: {
  title:"万能视频下载站 · 国际版", brand:"万能视频下载站", brandSub:"INTERNATIONAL · CLOUD EDITION",
  hero1a:"粘贴链接，", hero1b:"一键下载",
  heroP:"YouTube · TikTok · Twitter/X · Instagram · Vimeo 等 1000+ 站点 · 分辨率任选 · 浏览器直接下载",
  urlPh:"粘贴视频链接；多个链接（空格分隔）将按最佳画质逐个下载",
  parse:"解析下载", parsing:"解析中…",
  needUrl:"请先粘贴视频链接",
  parseOk:"解析成功，选择分辨率下载",
  parseFail:"解析失败", dlStart:"开始下载（浏览器下载栏可见进度）",
  recommended:"推荐", videoSec:"视频（含画面）", audioSec:"纯音频",
  optBest:"⭐ 最佳（推荐）", optAudio:"🎵 纯音频",
  resolution:"分辨率", dlSel:"⬇ 下载所选", willDl:"将下载：", unknownSize:"大小未知",
  site:"站点", author:"作者", duration:"时长", fmtsN:n=>`共 <b>${n}</b> 种格式`,
  audio:"音频", unknown:"未知", withAudio:" + 音频", audioBest:"最佳音频",
  bestMerged:"最佳画质（自动合并音视频）",
  plTitle:"批量队列", batchDone:(q,f)=>`批量完成：成功 ${q}，失败 ${f}`,
  bOk:"✅ 已开始", bRun:"⏳ 下载中…", bErr:"❌ 失败",
  foot1:"下载由服务器实时转发给浏览器 · 单文件建议 ≤ 2GB",
  foot2:"仅供个人学习交流，请尊重版权",
  reqFail:"请求失败 ",
},
en: {
  title:"Video Downloader · International", brand:"Video Downloader", brandSub:"INTERNATIONAL · CLOUD EDITION",
  hero1a:"Paste a link, ", hero1b:"download instantly",
  heroP:"YouTube · TikTok · Twitter/X · Instagram · Vimeo and 1000+ sites · pick resolution · saves straight to your browser",
  urlPh:"Paste a video link; multiple links (space separated) download at best quality",
  parse:"Download", parsing:"Parsing…",
  needUrl:"Paste a video link first",
  parseOk:"Parsed. Pick a resolution to download",
  parseFail:"Parse failed", dlStart:"Download started (see your browser downloads)",
  recommended:"Recommended", videoSec:"Video", audioSec:"Audio only",
  optBest:"⭐ Best (recommended)", optAudio:"🎵 Audio only",
  resolution:"Resolution", dlSel:"⬇ Download", willDl:"Will download: ", unknownSize:"size unknown",
  site:"Site", author:"Author", duration:"Duration", fmtsN:n=>`<b>${n}</b> formats`,
  audio:"audio", unknown:"unknown", withAudio:" + audio", audioBest:"Best audio",
  bestMerged:"Best quality (video+audio merged)",
  plTitle:"Batch queue", batchDone:(q,f)=>`Batch done: ${q} ok, ${f} failed`,
  bOk:"✅ started", bRun:"⏳ downloading…", bErr:"❌ failed",
  foot1:"Streamed through the server straight to your browser · single file ≤ 2GB recommended",
  foot2:"For personal study only. Please respect copyright.",
  reqFail:"Request failed ",
}};

let LANG = localStorage.getItem("ui_lang") ||
  (navigator.language && navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en");
function t(k){ const d = I18N[LANG] || I18N.zh; return d[k] !== undefined ? d[k] : (I18N.zh[k] !== undefined ? I18N.zh[k] : k); }

function applyLang(){
  document.title = t("title");
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = t(el.dataset.i18n));
  document.querySelectorAll("[data-i18n-ph]").forEach(el => el.placeholder = t(el.dataset.i18nPh));
  $("#langZh").classList.toggle("on", LANG === "zh");
  $("#langEn").classList.toggle("on", LANG === "en");
  if(LAST) render(LAST);
}
$("#langZh").onclick = ()=>{ LANG="zh"; localStorage.setItem("ui_lang","zh"); applyLang(); };
$("#langEn").onclick = ()=>{ LANG="en"; localStorage.setItem("ui_lang","en"); applyLang(); };

async function api(path, body){
  const h = {"X-UI-Lang": LANG};
  if(body){ h["Content-Type"] = "application/json"; }
  const r = await fetch(path, body ? {method:"POST", headers:h, body:JSON.stringify(body)} : {headers:h});
  const j = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error || (t("reqFail") + r.status));
  return j;
}

/* ---------- auth ---------- */
let ME = null, AUTH_AFTER = null, AUTH_MODE = "login";

async function refreshMe(){
  try{ ME = await api("/api/me"); }catch{ ME = {user:null, oauth:[], paid_required:false}; }
  renderUserChip();
}
function renderUserChip(){
  const el = $("#userChip");
  if(ME && ME.user){
    el.innerHTML = '<span class="chip">👤 ' + esc(ME.user.name) +
      (ME.user.paid ? ' <b style="color:var(--ok)">PRO</b>' : '') + '</span>' +
      '<button class="icon-btn" id="btnLogout">' + esc(LANG==="zh" ? "退出" : "Sign out") + '</button>';
    $("#btnLogout").onclick = async ()=>{ await api("/api/auth/logout", {}); ME.user = null; renderUserChip(); };
  }else{
    el.innerHTML = '<button class="icon-btn" id="btnLoginTop">' + esc(LANG==="zh" ? "登录 / 注册" : "Sign in") + '</button>';
    $("#btnLoginTop").onclick = ()=>openAuth(null);
  }
}
function openAuth(after){
  AUTH_AFTER = after;
  $("#authUser").value = ""; $("#authPass").value = ""; $("#authPass2").value = "";
  setAuthMode("login"); renderOauthBtns();
  $("#authMask").classList.add("show");
  $("#authUser").focus();
}
function setAuthMode(m){
  AUTH_MODE = m;
  $("#atabLogin").classList.toggle("on", m==="login");
  $("#atabReg").classList.toggle("on", m==="register");
  $("#regExtra").style.display = m==="register" ? "block" : "none";
  $("#btnSubmitAuth").textContent = m==="register" ? "Sign up" : "Sign in";
  authErr("");
}
$("#atabLogin").onclick = ()=>setAuthMode("login");
$("#atabReg").onclick = ()=>setAuthMode("register");
$("#authMask").onclick = e => { if(e.target.id==="authMask") $("#authMask").classList.remove("show"); };
function authErr(m){
  const el = $("#authErr");
  el.textContent = m || ""; el.style.display = m ? "block" : "none";
}
function renderOauthBtns(){
  const el = $("#oauthBtns");
  const provs = (ME && ME.oauth) || [];
  const divider = el.previousElementSibling, agree = el.nextElementSibling;
  if(!provs.length){
    el.style.display = "none";
    if(divider) divider.style.display = "none";
    if(agree) agree.style.display = "none";
    return;
  }
  el.style.display = "flex";
  if(divider) divider.style.display = "flex";
  if(agree) agree.style.display = "block";
  const svgs = {
    github: '<svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>',
    google: '<svg width="20" height="20" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.8C34.3 32.7 29.6 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.5 6.1 29.5 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.7-.4-3.9z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.5 6.1 29.5 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.6 0-10.3-3.3-11.8-8l-6.5 5C9.2 39.6 16.1 44 24 44z"/><path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.8c-.8 2.2-2.2 4.2-4.1 5.5l6.2 5.2C41.4 35.4 44 30.2 44 24c0-1.3-.1-2.7-.4-3.9z"/></svg>'
  };
  const names = {github:"GitHub", google:"Google"};
  el.innerHTML = provs.map(p=>'<a class="soc" href="/oauth/' + esc(p) + '/login" title="' +
    esc(names[p]||p) + '">' + (svgs[p] || esc(names[p]||p)) + '</a>').join("");
}
async function submitAuth(){
  const name = $("#authUser").value.trim(), pwd = $("#authPass").value;
  authErr("");
  if(name.length < 3 || name.length > 20) return authErr("Username: 3-20 characters");
  if(pwd.length < 6) return authErr("Password: at least 6 characters");
  if(AUTH_MODE==="register" && $("#authPass2").value !== pwd) return authErr("Passwords do not match");
  const btn = $("#btnSubmitAuth"); btn.disabled = true;
  try{
    const j = await api("/api/auth/" + AUTH_MODE, {username:name, password:pwd});
    ME.user = j.user;
    $("#authMask").classList.remove("show");
    renderUserChip();
    toast(AUTH_MODE==="register" ? "Account created ✓" : "Signed in ✓", "ok");
    const cb = AUTH_AFTER; AUTH_AFTER = null;
    if(cb) cb();
  }catch(e){ authErr(e.message); }
  btn.disabled = false;
}
$("#btnSubmitAuth").onclick = submitAuth;
[$("#authPass"), $("#authPass2")].forEach(inp=>inp.addEventListener("keydown", e=>{
  if(e.key==="Enter") submitAuth();
}));

function requireLogin(cb){
  if(ME && ME.user){ cb(); return; }
  openAuth(cb);
}

function toast(msg, type){
  const d = document.createElement("div");
  d.className = "toast"; d.style.cssText = "position:fixed;top:16px;left:50%;transform:translateX(-50%);" +
    "background:#161c2b;border:1px solid " + (type==="err" ? "rgba(248,113,113,.45)" : "rgba(52,211,153,.4)") +
    ";border-radius:12px;padding:11px 18px;font-size:13.5px;z-index:99;max-width:90vw";
  d.textContent = msg; document.body.appendChild(d);
  setTimeout(()=>d.remove(), 4500);
}

const fmtBytes = b => b==null ? "—" : b>=1073741824 ? (b/1073741824).toFixed(2)+" GB"
  : b>=1048576 ? (b/1048576).toFixed(1)+" MB" : b>=1024 ? (b/1024).toFixed(0)+" KB" : b+" B";
const fmtDur = s => { if(s==null) return "—"; s = Math.round(s);
  const h=~~(s/3600), m=~~(s%3600/60), ss=s%60;
  return h?`${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`:`${m}:${String(ss).padStart(2,'0')}`; };
const shortCodec = c => !c||c==="none" ? "" : String(c).split(".")[0];

let LAST = null;

function parseUrl(url){
  const urls = [...new Set((url.match(/https?:\/\/[^\s,，;；'"<>]+/gi) || []).map(x=>x.trim()))];
  if(!urls.length) return toast(t("needUrl"), "err");
  if(urls.length === 1) return parseOne(urls[0]);
  return batch(urls);
}

async function parseOne(url){
  const btn = $("#btnGo");
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + t("parsing");
  $("#resultArea").innerHTML = ""; LAST = null;
  try{
    const j = await api("/api/parse", {url});
    if(j.type === "playlist"){ renderPlaylist(j); }
    else if(j.thirdparty && j.type2 === "gallery"){ LAST = j; renderGallery(j); toast(t("parseOk"), "ok"); }
    else if(j.thirdparty){ LAST = j; renderThirdparty(j); toast(t("parseOk"), "ok"); }
    else { LAST = j; renderVideo(j); toast(t("parseOk"), "ok"); }
  }catch(e){
    $("#resultArea").innerHTML = `<div class="panel"><div class="err-box">❌ ${esc(t("parseFail"))}：${esc(e.message)}</div></div>`;
  }finally{
    btn.disabled = false; btn.textContent = t("parse");
    $("#resultArea").scrollIntoView({behavior:"smooth", block:"start"});
  }
}

function renderGallery(v){
  const imgs = v.images || [];
  const zipHref = "/api/zip?" + imgs.map(u=>"u="+encodeURIComponent(u)).join("&u=") +
                  "&name=" + encodeURIComponent((v.title||"gallery").slice(0,60));
  $("#resultArea").innerHTML = `
    <div class="panel">
      <h3 style="font-size:16px;word-break:break-all">${esc(v.title)}</h3>
      <div class="vmeta">
        <span class="tag">${esc(t("site"))} <b>${esc(v.extractor)}</b></span>
        <span class="tag">${esc(LANG==="zh" ? "图集 " + imgs.length + " 张" : imgs.length + " images")}</span>
      </div>
      ${v.thumbnail?`<div style="margin:12px 0"><img src="${esc(v.thumbnail)}" style="max-width:220px;border-radius:10px" referrerpolicy="no-referrer"></div>`:""}
      <div class="dl-bar" style="margin-top:14px">
        <a class="btn" style="text-decoration:none" href="${esc(zipHref)}">${esc(LANG==="zh" ? "⬇ 下载全部图片 (ZIP)" : "⬇ Download all (ZIP)")}</a>
        <span style="color:var(--muted);font-size:12.5px">${esc(LANG==="zh" ? "打包为 ZIP，共 " + imgs.length + " 张原图" : "ZIP with " + imgs.length + " full-size images")}</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:14px">
        ${imgs.map((u,i)=>`<a href="/api/stream?u=${encodeURIComponent(u)}&name=${encodeURIComponent((v.title||"gallery").slice(0,50))}-${i+1}&ext=jpg" download style="display:block">
          <img src="${esc(u)}" style="width:100%;border-radius:8px;aspect-ratio:3/4;object-fit:cover" referrerpolicy="no-referrer" loading="lazy">
        </a>`).join("")}
      </div>
    </div>`;
  $("#resultArea .panel").classList.add("flash");
}

function renderThirdparty(v){
function renderThirdparty(v){
  const d = v.direct || {};
  const mk = (id, label, ext) => d[id] ? `<a class="btn small" style="text-decoration:none" href="/api/stream?u=${encodeURIComponent(d[id])}&name=${encodeURIComponent((v.title||"video").slice(0,70))}&ext=${ext}">${label}</a>` : "";
  const bits = (v.formats||[]).filter(f=>/^bit\d+$/.test(f.format_id) && f.url);
  const bitBtns = bits.map(f=>`<a class="btn small" style="text-decoration:none" href="/api/stream?u=${encodeURIComponent(f.url)}&name=${encodeURIComponent((v.title||"video").slice(0,70))}&ext=mp4">${esc(f.label||f.format_id)}</a>`).join(" ");
  $("#resultArea").innerHTML = `
    <div class="panel">
      <h3 style="font-size:16px;word-break:break-all">${esc(v.title)}</h3>
      <div class="vmeta">
        <span class="tag">${esc(t("site"))} <b>${esc(v.extractor)}</b></span>
        <span class="tag">${esc(t("duration"))} <b>${fmtDur(v.duration)}</b></span>
        <span class="tag" style="color:var(--ok)">无水印直链</span>
      </div>
      ${v.thumbnail?`<div style="margin:12px 0"><img src="${esc(v.thumbnail)}" style="max-width:220px;border-radius:10px" referrerpolicy="no-referrer"></div>`:""}
      <div class="dl-bar" style="margin-top:14px">
        ${mk("hd", LANG==="zh"?"⬇ 下载无水印高清":"⬇ HD no-watermark", "mp4")}
        ${mk("wm", LANG==="zh"?"⬇ 带水印":"⬇ Watermarked", "mp4")}
        ${mk("mp3", LANG==="zh"?"🎵 音频 MP3":"🎵 Audio MP3", "mp3")}
      </div>
      ${bitBtns?`<div class="dl-bar" style="margin-top:10px"><span style="font-size:13px;color:var(--muted)">码率:</span> ${bitBtns}</div>`:""}
    </div>`;
  $("#resultArea .panel").classList.add("flash");
}

function render(v){
  const vids = v.formats.filter(f=>f.vcodec);
  const auds = v.formats.filter(f=>!f.vcodec);
  const bestId = "b";
  const row = f => `
    <label class="fmt-row${f._on?" on":""}">
      <input type="radio" name="fmt" value="${esc(f.format_id)}" data-label="${esc(fmtLabel(f))}" ${f._on?"checked":""}>
      <span class="fmt-main">${esc(fmtLabel(f))}</span>
      <span class="badge">${esc(f.ext||"?")}</span>
      <span class="fmt-size">${f.filesize?fmtBytes(f.filesize):t("unknownSize")}</span>
    </label>`;
  const byHeight = {};
  vids.forEach(f=>{ const h=f.height||0;
    if(h && (!byHeight[h] || (f.tbr||0)>(byHeight[h].tbr||0))) byHeight[h]=f; });
  const hs = Object.keys(byHeight).map(Number).sort((a,b)=>b-a);
  $("#resultArea").innerHTML = `
    <div class="panel">
      <h3 style="font-size:16px;word-break:break-all">${esc(v.title)}</h3>
      <div class="vmeta">
        <span class="tag">${esc(t("site"))} <b>${esc(v.extractor)}</b></span>
        ${v.uploader?`<span class="tag">${esc(t("author"))} <b>${esc(v.uploader)}</b></span>`:""}
        <span class="tag">${esc(t("duration"))} <b>${fmtDur(v.duration)}</b></span>
        <span class="tag">${t("fmtsN")(v.formats.length)}</span>
      </div>
      <div style="margin-top:10px">
        <div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">★ ${esc(t("recommended"))}</div>
        ${row({format_id:bestId, ext:"", filesize:null, _on:true, label:t("bestMerged"), height:null})}
      </div>
      ${vids.length?`<div style="margin-top:12px"><div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">${esc(t("videoSec"))}</div>
        ${vids.map(f=>row({...f,_on:false})).join("")}</div>`:""}
      ${auds.length?`<div style="margin-top:12px"><div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">${esc(t("audioSec"))}</div>
        ${auds.map(f=>row({...f,_on:false})).join("")}</div>`:""}
      <div class="dl-bar">
        <label style="font-size:13px;color:var(--muted)">${esc(t("resolution"))}</label>
        <select class="fmt-select" id="fmtSelect">
          <option value="b">${t("optBest")}</option>
          ${hs.map(h=>`<option value="${esc(byHeight[h].format_id)}">${h}p</option>`).join("")}
          ${auds.length?`<option value="ba">${t("optAudio")}</option>`:""}
        </select>
        <button class="btn" id="btnDl">⬇ ${esc(t("dlSel"))}</button>
        <span style="color:var(--muted);font-size:12.5px" id="dlPick"></span>
      </div>
    </div>`;
  const sel = document.getElementById("fmtSelect");
  sel.onchange = ()=>{ const r = [...document.querySelectorAll('input[name=fmt]')].find(x=>x.value===sel.value);
    if(r){ r.checked = true; r.dispatchEvent(new Event("change")); } };
  const upd = ()=>{ const el = $('input[name=fmt]:checked'); if(!el) return;
    document.querySelectorAll('input[name=fmt]').forEach(r=>r.closest(".fmt-row").classList.toggle("on", r.checked));
    $("#dlPick").textContent = t("willDl") + el.dataset.label;
    if([...sel.options].some(o=>o.value===el.value)) sel.value = el.value; };
  document.querySelectorAll('input[name=fmt]').forEach(r=>r.addEventListener("change", upd));
  upd();
  $("#resultArea .panel").classList.add("flash");
  $("#btnDl").onclick = ()=>{ location.href = streamUrl(v.webpage_url || $("#urlInput").value, sel.value, v.title); };
}

function fmtLabel(f){
  if(f.label) return f.label;
  if(!f.vcodec) return `${f.tbr?Math.round(f.tbr)+"kbps ":""}${t("audio")}`;
  let s = f.height ? f.height+"p" : (f.resolution||t("unknown"));
  if(f.fps && f.fps>31) s += ` ${Math.round(f.fps)}fps`;
  return s;
}
function streamUrl(url, fmt, title){
  return `/api/download?url=${encodeURIComponent(url)}&format=${encodeURIComponent(fmt)}&name=${encodeURIComponent((title||"video").slice(0,70))}`;
}

function renderPlaylist(p){
  $("#resultArea").innerHTML = `
    <div class="panel">
      <h2>${esc(p.title)} <span class="cnt">${p.count}</span>
        <button class="btn small" id="plAll" style="margin-left:auto">${esc(t("dlSel"))} ×${Math.min(p.count, 20)}</button>
      </h2>
      <div class="pl-list">
        ${p.entries.slice(0,50).map((e,i)=>`
          <div class="pl-row">
            <span class="idx">${i+1}</span>
            <span class="t" title="${esc(e.title)}">${esc(e.title)}</span>
            <span class="status st-run" id="pl-${i}">·</span>
          </div>`).join("")}
      </div>
      <div style="font-size:12px;color:var(--muted);margin-top:8px">${esc(t("batchNote")||"按最佳画质逐条开始浏览器下载")}</div>
    </div>`;
  $("#plAll").onclick = async ()=>{
    const items = p.entries.slice(0, 20);
    let q = 0, f = 0;
    for(let i=0; i<items.length; i++){
      const st = document.getElementById("pl-"+i);
      if(st) st.className = "status st-run", st.textContent = "⏳";
      try{
        const r = await fetch(streamUrl(items[i].url, "b", items[i].title), {headers:{"X-UI-Lang":LANG}});
        if(!r.ok) throw new Error(r.status);
        const blob = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = (items[i].title || ("video"+i)).slice(0,70) + ".mp4";
        a.click(); URL.revokeObjectURL(a.href);
        q++; if(st){ st.className="status st-ok"; st.textContent = "✅"; }
      }catch(e){
        f++; if(st){ st.className="status st-err"; st.textContent = "❌"; }
      }
    }
    toast(t("batchDone")(q, f), f?"err":"ok");
  };
}

async function batch(urls){
  if(!(ME && ME.user)){ openAuth(()=>batch(urls)); return; }   // 批量需登录
  $("#resultArea").innerHTML = `
    <div class="panel">
      <h2>${esc(t("plTitle"))} <span class="cnt">${urls.length}</span></h2>
      <div class="pl-list">
        ${urls.map((u,i)=>`<div class="pl-row"><span class="idx">${i+1}</span>
          <span class="t">${esc(u)}</span><span class="status st-run" id="pl-${i}">·</span></div>`).join("")}
      </div>
    </div>`;
  $("#resultArea").scrollIntoView({behavior:"smooth"});
  let q = 0, f = 0;
  for(let i=0; i<urls.length; i++){
    const st = document.getElementById("pl-"+i);
    try{
      const j = await api("/api/parse", {url:urls[i]});
      if(j.type === "video"){
        if(st) st.className = "status st-run", st.textContent = "⏳";
        const r = await fetch(streamUrl(j.webpage_url || urls[i], "b", j.title), {headers:{"X-UI-Lang":LANG}});
        if(!r.ok) throw new Error(r.status);
        const blob = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = (j.title || ("video"+i)).slice(0,70) + ".mp4";
        a.click(); URL.revokeObjectURL(a.href);
        q++; if(st){ st.className="status st-ok"; st.textContent = "✅"; }
      }else{ f++; if(st){ st.className="status st-err"; st.textContent = "❌"; } }
    }catch(e){ f++; if(st){ st.className="status st-err"; st.textContent = "❌"; } }
  }
  toast(t("batchDone")(q, f), f?"err":"ok");
}

$("#btnGo").onclick = ()=>parseUrl($("#urlInput").value);
$("#urlInput").addEventListener("keydown", e=>{ if(e.key==="Enter") parseUrl($("#urlInput").value); });
refreshMe();
applyLang();
