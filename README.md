# Cai Install Web GUI — OpenSteam 适配版

> **本项目是 [pvzcxw/Cai-install-Web-GUI](https://github.com/pvzcxw/Cai-install-Web-GUI) 的二次开发分支。**
>
> **原作者：[pvzcxw](https://github.com/pvzcxw)** — 原始项目 `Cai Install` 全系（Cai install XP / GUI / Web UI / Reborn）由 pvzcxw 独立开发，2023–2026 年间累计更新 70 余个版本。原作者已于 2026 年 6 月宣布全系停更并以 **MIT 协议** 开源，明确欢迎二改（"欢迎大家二改！继承 cai install 的 ip 精神！"）。
>
> 本分支仅在其基础上做**适配与修复**，不改变原作者身份与著作权。原始 LICENSE（MIT, Copyright (c) 2026 pvzcxw）完整保留。

---

## 📌 这是什么

在原版基础上增加对 **OpenSteam (OpenSteamKitten)** 的支持，并修复原版中已失效的清单源。

原版只支持 SteamTools 与 GreenLuma。因为 OpenSteamKitten 的 lua 语法与 SteamTools 完全一致（`addappid` / `setManifestid`），差异只在存放目录（`config\stplug-in` → `config\lua`），所以本分支复用原有生成逻辑，只抽出一层目录选择。

其余为清单源维护：移除已失效的源、补充可用源、修复端点变更。

**详细变更见 [Releases](../../releases)。**

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

### 自行构建

```powershell
pwsh -NoProfile -File build.ps1
```

采用 Nuitka `--onefile` x64，与原发行版打包方式一致。

---

## 💡 使用

1. **配置 GitHub Token（强烈建议）**
   设置 → 填入 GitHub Personal Access Token。匿名配额仅 60 次/小时，带 token 提升到 5000 次/小时。

2. **（OpenSteam 用户）确认解锁工具**
   设置 → "OpenSteamKitten 路径" 填 `C:\Other\Opensteam`（留空自动检测）。配置状态应显示 "已检测到 OpenSteam (OpenSteamKitten)"。

3. **搜索游戏并入库**
   首页输入游戏名或 AppID → 选清单源 → 勾选需要的选项 → 开始任务。lua 会写入 `<Steam>\config\lua\{appid}.lua`。

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
