# 万能视频下载站 · Vercel 国际版（部署指南）

架构：Serverless 流式直传——解析秒回，下载由 Vercel 函数从源站拉流、实时转发给用户浏览器（无磁盘、无队列、自带 HTTPS 和全球 CDN）。

## 部署步骤（10 分钟）

1. 把本目录 `vercel-deploy/` 推到一个 GitHub 仓库（或直接用 Vercel CLI）：
   - 网页方式：github.com/new 建仓库 → 上传 `api/`、`public/`、`requirements.txt`、`vercel.json`
   - CLI 方式：`npm i -g vercel` → 本目录里执行 `vercel --prod`
2. 打开 [vercel.com/new](https://vercel.com/new) → Import 你的仓库 → Framework 选 **Other** → Deploy
3. 部署完成得到 `https://你的项目.vercel.app` —— 完事

## 环境变量（可选）

| 变量 | 说明 |
|---|---|
| `PROXY_URL` | 函数访问源站使用的代理（默认直连） |
| `PRICE` | 预留 |

## 能力与限制（如实）

- ✅ YouTube / TikTok / Twitter / Instagram / Vimeo 等 1000+ 境外站点
- ✅ 分辨率选择、音视频自动合并（imageio-ffmpeg 内置）、纯音频
- ✅ 多链接批量（前端逐条流式下载）
- ❌ 国内站点（B站/小红书/抖音/快手）——境外节点 + 风控，已主动屏蔽并提示
- ⚠ 单文件建议 ≤ 2GB（函数时限内传不完会中断）；Vercel Hobby 函数 300 秒上限
- ⚠ 无登录/无付费（无数据库）；需要时接 Neon Postgres（免费）二期加回

## YouTube 风控解法（COOKIES）

YouTube 对数据中心 IP 有登录风控（解析报 "requires sign-in"）。解法：把浏览器里的 YouTube cookies 导出为 Netscape cookies.txt 格式（浏览器插件 Get cookies.txt），**用文本编辑器打开把全部内容**填到 Vercel 项目的 Settings → Environment Variables → 新增 `COOKIES` → Redeploy。之后 YouTube 解析/下载恢复正常（cookie 一般可维持数月）。

## 本地测试

```
pip install flask yt-dlp imageio-ffmpeg
python api/index.py     # http://127.0.0.1:8768
```
