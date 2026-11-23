# Cai Install Web GUI — OpenSteam 适配版

> **本项目是 [pvzcxw/Cai-install-Web-GUI](https://github.com/pvzcxw/Cai-install-Web-GUI) 的二次开发分支。**
>
> **原作者：[pvzcxw](https://github.com/pvzcxw)** — 原始项目 `Cai Install` 全系（Cai install XP / GUI / Web UI / Reborn）由 pvzcxw 独立开发，2023–2026 年间累计更新 70 余个版本。原作者已于 2026 年 6 月宣布全系停更并以 **MIT 协议** 开源，明确欢迎二改（"欢迎大家二改！继承 cai install 的 ip 精神！"）。
>
> 本分支仅在其基础上做**适配与修复**，不改变原作者身份与著作权。原始 LICENSE（MIT, Copyright (c) 2026 pvzcxw）完整保留。

---

## 📌 本分支做了什么

原版只支持 **SteamTools** 与 **GreenLuma** 两种解锁工具。本分支增加对 **[OpenSteam](https://github.com/OpenSteamTools) (OpenSteamKitten)** 的支持，并修复了一批已失效的远端数据源。

### 1. OpenSteam (OpenSteamKitten) 适配

OpenSteamKitten 的 lua 语法与 SteamTools 完全一致（`addappid` / `setManifestid`），差异只在存放目录：SteamTools 用 `config\stplug-in`，OpenSteamKitten 用 `config\lua`。

因此没有另起一套逻辑，而是在原有 SteamTools 生成流程上抽出一层目录选择：

- `_is_lua_unlocker(type)` — 判定是否为 lua 类解锁器（`steamtools` / `opensteam`）
- `_lua_write_dir(type)` — 返回对应写入目录（`config\stplug-in` 或 `config\lua`）

所有清单生成路径（GitHub 仓库、zip 源、创意工坊、不求人库、steamtools.games）统一走这两个函数，**写出内容与原 SteamTools 完全一致**，只是落到 `config\lua`。

新增能力：

- **自动检测**：读 `config.json` 的 `OpenSteam_Path`，或探测 `<Steam>\config\lua` 是否存在，或找 `OpenSteamKitten.exe`
- **设置项**：设置页新增 "OpenSteamKitten 路径" 输入框，留空则自动检测
- **强制模式**：`force_unlocker_type` 由 `auto / steamtools / greenluma` 扩展为 `auto / steamtools / greenluma / opensteam`
- **入库管理**：管理器新增 "OpenSteam" 标签页，与 SteamTools / GreenLuma 并列，支持查看、编辑、删除 lua、打开 lua 目录、强制入库/移除
- **重启联動**：`restart_steam()` 会先结束 `OpenSteamKitten.exe`，启动 Steam 后再拉起它
- **界面文案**：首页三个 lua 选项标签随当前解锁工具动态显示 "SteamTools:" 或 "OpenSteam:"

### 2. 清单源修复

原版内置的多个 zip 清单源已全部失效，实测状态：

| 源 | 状态 |
|---|---|
| `api.printedwaste.com` | HTTP 404 |
| `cysaw.top` | 0 字节空响应 |
| `furcate.eu` | Cloudflare 挑战页（非 zip） |
| `walftech.com` | HTTP 403 |
| `steamdatabase S3` | HTTP 404 |

已从源列表移除。当前可用源（9 项）：

- **自动搜索GitHub** — 并发查询所有内置+自定义仓库的分支
- **清单不求人库（仅清单）** — 端点已改版，见下
- **steamtools.games（6万+分支库）** — 新增，见下
- **SteamAutoCracks/ManifestHub(2)（仅密钥）** — 仅作密钥库
- **Sudama库（仅密钥）** — 仅作密钥库
- **GitHub (Auiowu)** — `Auiowu/ManifestAutoUpdate`，2591 分支
- **GitHub (ManifestHub3 主库)** — `steamtools-games/ManifestHub3`，62288 分支
- **GitHub (dc190 fork)** — `dc190/ManifestHub`，271 分支，与 ManifestHub3 不重合
- **GitHub (SAC)** — `SteamAutoCracks/ManifestHub`

### 3. 新增 steamtools.games 源

`steamtools.games` 站方提供公开 REST API（无需 key），后端是 6.2 万 appid 分支的 ManifestHub3：

- `POST /api/generate` body `{"appId": "<appid>"}` — 查询覆盖情况并返回成品 lua 地址
- `GET /api/files/{id}/lua` — 直接返回成品 lua
- `GET /api/files/{id}/key` — 返回 key.vdf
- `GET /api/files/{id}/zip` — 返回 lua + key.vdf + manifest 打包

这是补冷门游戏（含成人游戏）最有效的库。实现见 `process_steamtools_games_manifest()`。

### 4. 清单不求人库端点修复

原代码走 `POST https://manifest.steam.run/api/session` + `/api/request-code`，两个端点均已 404。从站方前端 JS 反查出真实端点已改为 **GET**：

```
GET https://manifest.steam.run/api/download_manifest?depot_id={depot}&manifest_id={manifest}
```

无需 token。`_download_manifest_buqiuren()` 与 `download_workshop_manifest()` 已改为直连此端点。

顺带修复：`buqiuren` 之前不在 `zip_sources` 列表里，被误当成 GitHub 分支仓库处理必然 404；且原实现只下载 `.manifest` 不生成 lua，对 OpenSteam 等于没解锁——现已补上 lua 生成。

### 5. dpotkey 补全（dc190 这类库）

`dc190/ManifestHub` 这类库的分支 zip 里**没有 `key.vdf`**，只有 `{appid}.json`（`has_key: false`）、`{appid}.lua`（`addappid` 行不带密钥）和 `.manifest`。原代码只读 `key.vdf`，会生成缺 key、无法解锁的 lua。

修复：两条生成路径都增加"从分支内 lua 取 depot 列表 + 从密钥库补 key"逻辑。实测 `2928600` 从 113B 无效 lua 变为 293B 有效 lua，2 个 depot 全部带 key。

### 6. 数据源三级回退

`steamui.com` 与 `steam.ddxnb.cn` 已分别返回 403，原有两级回退全部失败导致任务必定报错。新增第三级 `api.steamcmd.net`（实测可用，返回结构与 ddxnb 一致）：

```
steamui.com → steam.ddxnb.cn → api.steamcmd.net
```

### 7. 封面图修复

Steam 已把图片路径从 `cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg` 迁移到 `shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/{hash}/header_{lang}.jpg`。**2026 年后的新游戏在旧路径下完全没有图片**（5175310 全部尺寸 404）。

后端新增 `/api/cover/<appid>` 代理路由：先查本地缓存，再调 `appdetails` 取 `header_image`，回退新旧多条候选路径，域名白名单校验 + JPEG magic 校验。前端 `manager.js` 改为请求此代理。

### 8. 其他修复

- **GitHub API 配额检查**：网络不通时原逻辑 `return False` 会中断整条任务链，改为 warning + 继续
- **zip 有效性校验**：源返回非 zip 内容时（如 2 字节 `OK`）原本抛 `BadZipFile` 崩掉任务，改为校验后返回友好错误
- **codeload 免配额通道**：GitHub API 拿分支失败时改用 `codeload.github.com/{repo}/zip/refs/heads/{appid}`，不消耗 API 配额
- **密钥库多源回退**：Sudama API 失败时回退 `raw.githubusercontent.com/SteamAutoCracks/ManifestHub/main/depotkeys.json`（28 万条）
- **重复 SocketIO 初始化**：`app.py` 有两处 `SocketIO(...)`，后者覆盖前者丢了 `async_mode='threading'`，已删除重复行

### 9. 新增：自动端口

设置页 "应用程序配置" 新增 **自动端口** 开关。开启后启动时不再弹出 tkinter 端口询问窗口，自动从 5000 起挑选第一个可用端口（避开占用）。

关闭时行为与原版完全一致：弹窗询问，默认 5000。命令行参数 `--port` / `--port=` 与环境变量 `CAI_PORT` 优先级高于该开关。

### 10. 构建方式对齐发行版

原版发行版 `cai.install.web.ui2.5.exe`（19.26 MB）为 **Nuitka `--onefile` x64**（MinGW 编译，含 `NUITKA_ONEFILE_START`/`PARENT`/`DIRECTORY` 标记，无 PyInstaller 特征）。

本分支提供 `build.ps1`，产物同为 Nuitka `--onefile` x64 / PE machine `0x8664` / subsystem 3，打包特征与官方发行版一致：

```powershell
pwsh -NoProfile -File build.ps1
# 产物: cai.install.web.ui2.6.exe  (~16.6 MB)
```

---

## 🛠️ 安装与运行

### 环境要求

- **操作系统**：Windows 10 / 11
- **浏览器**：Chrome / Edge / Firefox
- **依赖软件**：SteamTools、GreenLuma 或 OpenSteam(OpenSteamKitten) 任一
- **开发环境**（仅源码运行）：Python 3.8+

### 方式一：下载发行版

前往 [Releases](../../releases) 下载 `.exe`，双击运行。

### 方式二：从源码运行

```bash
git clone https://github.com/kifoii/Cai-install-Web-GUI.git
cd Cai-install-Web-GUI
pip install Flask Flask-SocketIO httpx aiofiles colorlog vdf ujson
python app.py
```

启动时会弹出端口选择窗口（默认 `5000`）。若已在设置里开启"自动端口"，则跳过询问自动选端口。

无界面/脚本启动可用：

```bash
python app.py --port 5000
# 或
set CAI_NO_GUI=1 && python app.py
```

---

## 💡 使用指南

1. **配置 GitHub Token（强烈建议）**
   设置 → 填入 GitHub Personal Access Token。GitHub API 匿名配额仅 60 次/小时，带上 token 提升到 5000 次/小时。

2. **（OpenSteam 用户）确认解锁工具**
   设置 → "OpenSteamKitten 路径" 填 `C:\Other\Opensteam`（留空自动检测）。配置状态应显示 "已检测到 OpenSteam (OpenSteamKitten)"。

3. **搜索游戏并入库**
   首页输入游戏名或 AppID → 选清单源 → 勾选需要的选项 → 开始任务。lua 会写入 `C:\Other\Steam\config\lua\{appid}.lua`。

4. **管理器查看**
   访问 `/manager` → OpenSteam 标签页，可看到已入库游戏卡片（带封面）、编辑 lua、一键删除。

---

## 📁 项目结构

```
Cai-install-Web-GUI/
├── app.py              # Flask + SocketIO 主程序、路由
├── backend.py          # 核心逻辑：清单下载、lua 生成、管理器
├── build.ps1           # Nuitka --onefile 构建脚本
├── templates/          # index / manager / settings / about
├── static/
│   ├── js/             # app / manager / settings / background / theme
│   └── css/            # style / manager / settings / about
├── assets/             # 截图
├── LICENSE             # MIT (Copyright (c) 2026 pvzcxw)
└── README.md
```

---

## ⚠️ 免责声明

继承原项目声明的立场：

- **完全免费开源**：本工具及其源代码完全免费，基于 MIT 协议开源。
- **禁止倒卖**：原作者在 README 中明确提到，存在无良商家（B站某同名资源站、闲鱼倒卖者等）盗用本开源代码及免费清单库，加入卡密系统收费倒卖。请勿花冤枉钱。
- **风险自负**：本工具仅供代码学习与技术交流。使用本工具对 Steam 客户端进行修改所带来的一切后果，由使用者自行承担。

---

## 🤝 鸣谢

**本项目全部核心逻辑来自原作者 [pvzcxw](https://github.com/pvzcxw) 的开源项目 [Cai-install-Web-GUI](https://github.com/pvzcxw/Cai-install-Web-GUI)，本分支仅做适配与修复。**

原项目鸣谢名单（保留）：

- **核心逻辑**：[Cai-install](https://github.com/pvzcxw/Cai-install) (by [pvzcxw](https://github.com/pvzcxw))
- **UI 灵感**：[Onekey_GUI](https://github.com/qwq-xinkeng/Onekey_GUI) (by qwq-xinkeng)
- **VDF Writer**：由 `KS-MLC` 提供技术支持
- **DLC 检索及入库**：由 `B-I-A-O` 提供技术支持
- **清单不求人**：由 `☆☆☆☆` 提供技术支持
- **清单源与技术支持**：感谢 `FQQ`, `oureveryday`, `blanktming`, `wxy1343`, `Auiowu`, `宏` 等大佬的帮助。

本分支新增的清单源：

- **[steamtools.games](https://steamtools.games)** — 提供公开 REST API 与 ManifestHub3 数据库
- **`dc190/ManifestHub`**、**`steamtools-games/ManifestHub3`** — 清单库

---

## 📜 许可证

本项目采用 [MIT License](LICENSE) 开源。

```
MIT License
Copyright (c) 2026 pvzcxw
```

原始版权归 **pvzcxw** 所有。本分支的修改同样以 MIT 协议发布，保留原作者署名。

---

<div align="center">

**原项目已停更，本分支为社区适配维护。**

如果这个分支对你有帮助，请先给原作 [pvzcxw/Cai-install-Web-GUI](https://github.com/pvzcxw/Cai-install-Web-GUI) 一个 ⭐

</div>
