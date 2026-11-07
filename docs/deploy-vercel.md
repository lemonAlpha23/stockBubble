# 部署到 Vercel

看板是「FastAPI + 静态前端」的单进程应用，Vercel 没有常驻进程，所以整站被塞进**一个** Python 函数：`api/index.py` 把 `src/` 加进 import 路径后直接复用 `stockbubble.api.asgi:app`，前端由 app 里已有的 `StaticFiles` 托管（和本地一致）。`vercel.json` 只做两件事：给函数 60s 超时 + 把 `src/ config/ web/` 一起打进 bundle。

```
vercel.json          # functions.includeFiles + 全路径 rewrite 到函数
api/index.py         # 函数入口（sys.path / 绝对路径 / 默认 yahoo 源）
requirements.txt     # Vercel 只认这个（本地仍用 uv + pyproject.toml）
.vercelignore        # 不上传 .venv、tests、docs、tmp-*
```

## 一、先推到 GitHub（推荐）

```bash
cd D:\code\stockBubble
git add -A
git commit -m "feat: stock bubble dashboard"
git branch -M main
git remote add origin https://github.com/<you>/stockBubble.git
git push -u origin main
```

然后在 Vercel 里 **Add New → Project → Import Git Repository → 选这个仓库 → Deploy**，框架预设选 **Other**，Build Command 留空、Output Directory 留空、Install Command 留空（Vercel 会自动 `pip install -r requirements.txt`，Node 侧不需要构建）。

不需要任何环境变量；如果要改缓存或标题，加 `BUBBLE_*` 即可（见 README 的配置表）。

## 二、或者不用 GitHub，直接 CLI

```bash
npm i -g vercel
vercel login
vercel            # 第一次会问项目名 / scope，生成预览部署
vercel --prod     # 出正式域名
```

CLI 会把当前目录打包上传（`.vercelignore` 已排除 `.venv`、`tests`、`docs`）。

## 三、部署后要接受的三个事实

1. **冷启动会重新取数**：`TTLCache` 是进程内内存，函数实例回收（几十秒没请求）后第一次请求要重新打上游 —— 加密约 1.2s，美股约 6s（crumb + 258 只批量 + spark）。我给了 `maxDuration: 60`，热实例下 `/api/bubbles` 通常 0.05–0.3s。
2. **OpenBB 没装**：它是可选的慢路由（两百多个包、单只票 1s），函数入口里显式 `BUBBLE_US_PROVIDER=yahoo`，走批量源。
3. **静态资源走函数**：`/`、`/app.js`、`/vendor/*` 都由这个函数返回，没有 CDN 缓存。想改成「静态走 CDN、只有 `/api/*` 走函数」，把 `vercel.json` 换成：

   ```json
   {
     "outputDirectory": "web",
     "functions": { "api/index.py": { "maxDuration": 60, "memory": 1024, "includeFiles": ["src/**", "config/**"] } },
     "rewrites": [{ "source": "/api/(.*)", "destination": "/api/index.py" }]
   }
   ```

   这样 `web/` 变成静态输出，`/vendor/*` 能吃满 CDN，但 `/` 必须能直接落到 `web/index.html`（Vercel 在 `outputDirectory` 里会这么做）。

## 四、本地先自检

```bash
uv run python scripts/check_vercel_entry.py   # 用 ASGITransport 直接打 api/index.py 里的 app
# 或者整站跑起来：uv run uvicorn api.index:app --port 8078
```

本机实测（2026-11-07）：

```
OK   /api/health                                      200 (212B)
OK   /api/universes                                   200 (471B)
OK   /api/bubbles?universe=crypto&window=24h&limit=5   200 (2405B)
OK   /                                            200 (8020B)
OK   /app.js                                      200 (76372B)
OK   /vendor/planck.min.js                         200 (297472B)
us_equity: 3 nodes, warnings=[]
```

## 五、如果 `functions` 配置被拒（老账号/旧 schema）

把 `vercel.json` 换成 legacy `builds` 写法（`builds` 和 `functions` 不能同时出现）：

```json
{
  "version": 2,
  "builds": [
    { "src": "web/**", "use": "@vercel/static" },
    { "src": "api/index.py", "use": "@vercel/python", "config": { "maxLambdaSize": "60mb", "includeFiles": ["src/**", "config/**"] } }
  ],
  "routes": [
    { "src": "/api/(.*)", "dest": "/api/index.py" },
    { "src": "/(.*)", "dest": "/web/$1" }
  ]
}
```

这种情况下 `/`、`/app.js`、`/vendor/*` 由 CDN 直接出，`/api/*` 才进函数 —— 反而更快，只是 `maxDuration` 就没法在 `builds` 里配（默认 10s，冷启动美股那 6s 要小心）。

## 六、常见报错对照

| 现象 | 原因 / 处理 |
| --- | --- |
| `Function Runtimes must have a valid version` | `vercel.json` 里同时写了 `builds` 和 `functions`，Vercel 不允许；只保留 `functions` |
| `ModuleNotFoundError: stockbubble` | `includeFiles` 少了 `src/**`，或入口没有把 `src/` 插进 `sys.path` |
| `No universe found` / `universes_dir` 不存在 | `includeFiles` 少了 `config/**`；入口里已用 `BUBBLE_UNIVERSES_DIR` 钉绝对路径 |
| 首屏空白但 API 正常 | 静态文件没打包：`includeFiles` 要带上 `web/**` |
| 504 / FUNCTION_INVOCATION_TIMEOUT | 冷启动撞上 Yahoo 慢响应；确认 `maxDuration: 60`，或减小 `BUBBLE_MAX_NODES`、把 `limit` 默认值调小 |
