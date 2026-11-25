import sys
import os
import traceback
import time
import logging
import subprocess
import asyncio
import re
import aiofiles
import random
import string
import colorlog
import httpx
import winreg
import ujson as json
import vdf
import zipfile
import shutil
import struct
import zlib
import io  # For workshop manifest processing
from pathlib import Path
from typing import Tuple, Any, List, Dict, Literal
from urllib.parse import quote

CURRENT_VERSION = "2.6"  # 当前版本号
GITHUB_REPO = "kifoii/Cai-install-Web-GUI"

# --- OpenSteam(OpenSteamKitten) ---
OPENSTEAM_EXE_NAME = "OpenSteamKitten.exe"      # OpenSteam 解锁工具可执行文件名
OPENSTEAM_LUA_SUBDIR = "config/lua"             # OpenSteamKitten 的 lua 配置目录（相对 Steam 根目录）

# --- LOGGING SETUP ---
LOG_FORMAT = '%(log_color)s%(message)s'
LOG_COLORS = {
    'INFO': 'cyan',
    'WARNING': 'yellow',
    'ERROR': 'red',
    'CRITICAL': 'purple',
}

# --- MODIFIED: Added Custom_Repos setting ---
DEFAULT_CONFIG = {
    "Github_Personal_Token": "",
    "Custom_Steam_Path": "",
    "debug_mode": False,
    "logging_files": True,
    "background_image_path": "",
    "background_blur": 0,
    "background_saturation": 100,
    "background_brightness": 80, 
    "show_console_on_startup": False,
    "auto_port": False,
    "force_unlocker_type": "auto",
    "Custom_Repos": {
        "github": [],
        "zip": []
    },
    "QA1": "温馨提示: Github_Personal_Token(个人访问令牌)可在Github设置的最底下开发者选项中找到, 详情请看教程。",
    "QA2": "Force_Unlocker: 强制指定解锁工具, 填入 'steamtools' 或 'greenluma'。留空则自动检测。",
    "QA3": "Custom_Repos: 自定义清单库配置。github数组用于添加GitHub仓库，zip数组用于添加ZIP清单库。",
    "QA4": "GitHub仓库格式: {\"name\": \"显示名称\", \"repo\": \"用户名/仓库名\"}",
    "QA5": "ZIP清单库格式: {\"name\": \"显示名称\", \"url\": \"下载URL，用{app_id}作为占位符\"}"
}

class STConverter:
    def __init__(self):
        self.logger = logging.getLogger('STConverter')

    def convert_file(self, st_path: str) -> str:
        try:
            content, _ = self.parse_st_file(st_path)
            return content
        except Exception as e:
            self.logger.error(f'ST文件转换失败: {st_path} - {e}')
            raise

    def parse_st_file(self, st_file_path: str) -> Tuple[str, dict]:
        with open(st_file_path, 'rb') as stfile:
            content = stfile.read()
        if len(content) < 12: raise ValueError("文件头过短")
        header = content[:12]
        xorkey, size, xorkeyverify = struct.unpack('III', header)
        xorkey ^= 0xFFFEA4C8
        xorkey &= 0xFF
        encrypted_data = content[12:12+size]
        if len(encrypted_data) < size: raise ValueError("加密数据小于预期大小")
        data = bytearray(encrypted_data)
        for i in range(len(data)):
            data[i] ^= xorkey
        decompressed_data = zlib.decompress(data)
        lua_content = decompressed_data[512:].decode('utf-8')
        metadata = {'original_xorkey': xorkey, 'size': size, 'xorkeyverify': xorkeyverify}
        return lua_content, metadata

class CaiBackend:
    # 环境信息（Steam 路径 / 解锁工具）每个进程只输出一次，避免请求级重复刷日志
    _env_logged = False

    def __init__(self):
        self.project_root = Path.cwd()
        self.client: httpx.AsyncClient | None = None
        self.config = {}
        self.steam_path = None
        self.unlocker_type = None
        self.lock = asyncio.Lock()
        self.temp_path = self.project_root / 'temp'
        self.log = self._init_log()
        self.name_cache: Dict[str, str] = {}
        self.cover_cache: Dict[str, str] = {}

    async def __aenter__(self):
        self.client = httpx.AsyncClient(verify=False, trust_env=True)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    def _init_log(self, level=logging.INFO) -> logging.Logger:
        logger = logging.getLogger(' Cai install')
        logger.setLevel(level)
        if not logger.handlers:
            stream_handler = colorlog.StreamHandler()
            stream_handler.setLevel(level)
            fmt = colorlog.ColoredFormatter(LOG_FORMAT, log_colors=LOG_COLORS)
            stream_handler.setFormatter(fmt)
            logger.addHandler(stream_handler)
        return logger

    def _configure_logger(self, verbose: bool = True):
        if not self.config:
            self.log.warning("无法应用日志配置，因为配置尚未加载。")
            return
        is_debug = self.config.get("debug_mode", False)
        level = logging.DEBUG if is_debug else logging.INFO
        self.log.setLevel(level)
        for handler in self.log.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(level)
        if verbose:
            self.log.debug(f"日志等级已设置为: {'DEBUG' if is_debug else 'INFO'}")
        self.log.handlers = [h for h in self.log.handlers if not isinstance(h, logging.FileHandler)]
        if self.config.get("logging_files", True):
            logs_dir = self.project_root / 'logs'
            logs_dir.mkdir(exist_ok=True)
            log_file_path = logs_dir / f'cai-install-gui-{time.strftime("%Y-%m-%d")}.log'
            file_handler = logging.FileHandler(log_file_path, 'a', encoding='utf-8')
            file_handler.setLevel(level)
            file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            file_handler.setFormatter(file_formatter)
            self.log.addHandler(file_handler)
            if verbose:
                self.log.info(f"已启用文件日志: {log_file_path}")
        elif verbose:
            self.log.info("文件日志已禁用。")

    def _compare_versions(self, v1: str, v2: str) -> int:
        """比较版本号，返回 -1, 0, 1"""
        try:
            import re
            
            def parse_version(v):
                # 分离主版本号和后缀
                match = re.match(r'(\d+(?:\.\d+)*)(.*)', v)
                if not match:
                    return (0, 0, 0), ''
                
                version_nums = match.group(1)
                suffix = match.group(2)
                
                # 解析版本号
                parts = version_nums.split('.')
                # 填充到3位
                while len(parts) < 3:
                    parts.append('0')
                
                # 转换为整数元组
                version_tuple = tuple(int(p) for p in parts[:3])
                
                return version_tuple, suffix
            
            v1_tuple, v1_suffix = parse_version(v1)
            v2_tuple, v2_suffix = parse_version(v2)
            
            # 首先比较主版本号
            if v1_tuple < v2_tuple:
                return -1
            elif v1_tuple > v2_tuple:
                return 1
            
            # 版本号相同，比较后缀
            # 空后缀被认为是正式版本，高于带后缀的版本
            if not v1_suffix and v2_suffix:
                return 1
            elif v1_suffix and not v2_suffix:
                return -1
            elif v1_suffix < v2_suffix:
                return -1
            elif v1_suffix > v2_suffix:
                return 1
            
            return 0
            
        except Exception as e:
            self.log.warning(f"版本比较失败: {e}")
            return 0
    
    async def check_for_updates(self) -> Tuple[bool, Dict]:
        """
        检查是否有新版本可用
        返回: (是否有更新, 版本信息字典)
        """
        try:
            self.log.info("正在检查更新...")
            
            # GitHub API URL
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            
            # 获取 GitHub token（如果有的话）
            github_token = self.config.get("Github_Personal_Token", "").strip()
            headers = {'Authorization': f'Bearer {github_token}'} if github_token else {}
            
            # 添加 User-Agent 以避免 API 限制
            headers['User-Agent'] = 'Cai-Install-Updater'
            
            # 发送请求
            response = await self.client.get(api_url, headers=headers, timeout=10)
            
            if response.status_code == 404:
                # 没有发布版本
                self.log.info("未找到发布版本")
                return False, {}
            
            response.raise_for_status()
            release_data = response.json()
            
            # 提取版本信息
            latest_version = release_data.get('tag_name', '').strip()
            if latest_version.startswith('v'):
                latest_version = latest_version[1:]  # 去掉 'v' 前缀
            
            release_name = release_data.get('name', '')
            release_body = release_data.get('body', '')
            release_url = release_data.get('html_url', '')
            published_at = release_data.get('published_at', '')
            
            # 获取下载链接
            download_urls = []
            assets = release_data.get('assets', [])
            for asset in assets:
                download_urls.append({
                    'name': asset.get('name', ''),
                    'url': asset.get('browser_download_url', ''),
                    'size': asset.get('size', 0)
                })
            
            # 如果没有 assets，使用 zipball_url
            if not download_urls and release_data.get('zipball_url'):
                download_urls.append({
                    'name': 'Source code (zip)',
                    'url': release_data.get('zipball_url', ''),
                    'size': 0
                })
            
            # 比较版本
            if self._compare_versions(CURRENT_VERSION, latest_version) < 0:
                self.log.info(f"发现新版本: {latest_version} (当前版本: {CURRENT_VERSION})")
                return True, {
                    'current_version': CURRENT_VERSION,
                    'latest_version': latest_version,
                    'release_name': release_name,
                    'release_body': release_body,
                    'release_url': release_url,
                    'published_at': published_at,
                    'download_urls': download_urls
                }
            else:
                self.log.info(f"当前已是最新版本 ({CURRENT_VERSION})")
                return False, {}
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                self.log.warning("GitHub API 请求次数已用尽，跳过更新检查")
            else:
                self.log.warning(f"检查更新时 HTTP 错误: {e}")
            return False, {}
        except httpx.TimeoutException:
            self.log.warning("检查更新超时，跳过")
            return False, {}
        except Exception as e:
            self.log.warning(f"检查更新失败: {e}")
            return False, {}

    async def initialize(self) -> Literal["steamtools", "greenluma", "opensteam", "conflict", "none", None]:
        if not self.config: self.config = await self.load_config()
        if self.config is None: return None
        # initialize() 每个 HTTP 请求都会跑一次，环境信息只打一次，避免日志被重复刷屏
        verbose = not CaiBackend._env_logged
        self._configure_logger(verbose=verbose)
        
        self.steam_path = self.get_steam_path()
        if not self.steam_path or not self.steam_path.exists():
            self.log.error('无法确定有效的Steam路径。请在设置中手动指定。')
            return None
        if verbose:
            self.log.info(f"Steam路径: {self.steam_path}")

        force_unlocker = self.config.get("force_unlocker_type", "auto")

        if force_unlocker in ["steamtools", "greenluma", "opensteam"]:
            self.unlocker_type = force_unlocker
            if verbose:
                self.log.info(f"已根据配置强制使用解锁工具: {force_unlocker.capitalize()}")
        else:
            is_steamtools = self._detect_steamtools()
            is_greenluma = any((self.steam_path / dll).exists() for dll in ['GreenLuma_2025_x86.dll', 'GreenLuma_2025_x64.dll'])
            is_opensteam = self._detect_opensteam()
            detected = [name for name, ok in (("steamtools", is_steamtools), ("greenluma", is_greenluma), ("opensteam", is_opensteam)) if ok]
            if len(detected) >= 2:
                if verbose:
                    self.log.error(f"环境冲突：同时检测到 {len(detected)} 种解锁工具 ({', '.join(detected)})！请在设置中强制指定一个。")
                self.unlocker_type = "conflict"
            elif is_opensteam:
                if verbose: self.log.info("自动检测到解锁工具: OpenSteam (OpenSteamKitten)")
                self.unlocker_type = "opensteam"
            elif is_steamtools:
                if verbose: self.log.info("自动检测到解锁工具: SteamTools")
                self.unlocker_type = "steamtools"
            elif is_greenluma:
                if verbose: self.log.info("自动检测到解锁工具: GreenLuma")
                self.unlocker_type = "greenluma"
            else:
                if verbose: self.log.warning("未能自动检测到解锁工具，将使用标准模式（可能需要手动配置）。")
                self.unlocker_type = "none"

        CaiBackend._env_logged = True

        try:
            (self.steam_path / 'config' / 'stplug-in').mkdir(parents=True, exist_ok=True)
            (self.steam_path / 'AppList').mkdir(parents=True, exist_ok=True)
            (self.steam_path / 'depotcache').mkdir(parents=True, exist_ok=True)
            # Create config/depotcache for workshop manifests
            (self.steam_path / 'config' / 'depotcache').mkdir(parents=True, exist_ok=True)
            # Create OpenSteamKitten lua directory (config/lua)
            (self.steam_path / OPENSTEAM_LUA_SUBDIR).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log.error(f"创建Steam子目录时失败: {e}")

        return self.unlocker_type

    def _detect_steamtools(self) -> bool:
        """检测 SteamTools 是否处于启用状态。

        仅凭 config/stplug-in 目录存在不能判定（该目录可能只是历史残留或空目录），
        需满足以下任一条件：
        1. config/stplug-in 下存在实际 .lua 解锁文件；
        2. Steam 根目录存在 SteamTools 的 core 注入文件（SteamTools.dll / SteamTools.Cfg 等）；
        3. 同级目录存在 SteamTools.exe 安装。
        注意：SteamTools 与 OpenSteam 会共用部分注入 dll 名，故优先用 lua 文件判定。
        """
        if not self.steam_path:
            return False

        st_dir = self.steam_path / 'config' / 'stplug-in'
        if st_dir.is_dir():
            try:
                if any(p.suffix.lower() == '.lua' for p in st_dir.iterdir() if p.is_file()):
                    return True
            except OSError:
                pass

        return False

    def _find_opensteam_exe_under(self, base: Path | None) -> Path | None:
        """在 base 目录本身及其一层子目录中查找 OpenSteamKitten.exe。

        子目录只检查名字含 "opensteam" 的（例如 `C:\\Other\\Opensteam`），
        避免对整个父目录做全量遍历。
        """
        if not base:
            return None
        try:
            direct = base / OPENSTEAM_EXE_NAME
            if direct.is_file():
                return direct
        except OSError:
            pass
        try:
            children = list(base.iterdir())
        except OSError:
            return None
        for child in children:
            try:
                if not child.is_dir() or 'opensteam' not in child.name.lower():
                    continue
                candidate = child / OPENSTEAM_EXE_NAME
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def _detect_opensteam(self) -> bool:
        """检测 OpenSteam(OpenSteamKitten) 是否可用，无需任何手动配置。

        判定条件（满足其一即可）：
        1. Steam 根目录存在 OpenSteam 注入 dll（OpenSteamTool.dll）；
        2. Steam 根目录下存在 config/lua 目录且其中有 .lua 解锁文件；
        3. Steam 根目录、或其父目录、或其父目录下名字含 "opensteam" 的子目录中
           存在 OpenSteamKitten.exe。
        """
        if not self.steam_path:
            return False

        # 1. 注入 dll 存在即视为已部署 OpenSteam
        if (self.steam_path / 'OpenSteamTool.dll').is_file():
            return True

        # 2. config/lua 目录内有实际 lua 解锁文件
        lua_dir = self.steam_path / OPENSTEAM_LUA_SUBDIR
        if lua_dir.is_dir():
            try:
                if any(p.suffix.lower() == '.lua' for p in lua_dir.iterdir() if p.is_file()):
                    return True
            except OSError:
                pass

        # 3. 兜底：按可执行文件位置判断
        return self._get_opensteam_exe_path() is not None

    def _is_lua_unlocker(self, unlocker_type: str) -> bool:
        """是否为基于 lua 文件的解锁工具（SteamTools / OpenSteam）。"""
        return unlocker_type in ("steamtools", "opensteam")

    def _lua_write_dir(self, unlocker_type: str) -> Path:
        """返回指定解锁工具的 lua 写入目录。
        SteamTools -> <Steam>/config/stplug-in
        OpenSteam  -> <Steam>/config/lua
        """
        if unlocker_type == "opensteam":
            return self.steam_path / OPENSTEAM_LUA_SUBDIR
        return self.steam_path / 'config' / 'stplug-in'

    def _get_opensteam_exe_path(self) -> Path | None:
        """定位 OpenSteamKitten.exe 可执行文件路径，完全依靠自动探测。

        搜索顺序：Steam 根目录 → Steam 父目录 → Steam 父目录下名字含
        "opensteam" 的子目录（常见布局 `C:\\Other\\Steam` +
        `C:\\Other\\Opensteam\\OpenSteamKitten.exe`）。
        """
        if not self.steam_path:
            return None
        for base in (self.steam_path, self.steam_path.parent):
            found = self._find_opensteam_exe_under(base)
            if found:
                return found
        return None

    def stack_error(self, exception: Exception) -> str:
        return ''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))

    async def gen_config_file(self):
        config_path = self.project_root / 'config.json'
        try:
        # 确保目录存在
            config_path.parent.mkdir(exist_ok=True, parents=True)
        
            with open(config_path, mode="w", encoding="utf-8") as f:
                f.write(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
            self.log.info('未识别到config.json，可能为首次启动，已自动生成，若进行配置重启生效')
        except Exception as e:
            self.log.error(f'生成配置文件失败: {self.stack_error(e)}')
    
    async def load_config(self) -> Dict | None:
        config_path = self.project_root / 'config.json'
        if not config_path.exists():
            await self.gen_config_file()
            return DEFAULT_CONFIG

        try:
            async with aiofiles.open(config_path, mode="r", encoding="utf-8") as f:
                # --- MODIFIED: Load config and merge with defaults to handle new keys ---
                user_config = json.loads(await f.read())
                config = DEFAULT_CONFIG.copy()
                config.update(user_config)
                
                # --- NEW: Ensure Custom_Repos structure exists ---
                if 'Custom_Repos' not in config:
                    config['Custom_Repos'] = {"github": [], "zip": []}
                elif not isinstance(config['Custom_Repos'], dict):
                    config['Custom_Repos'] = {"github": [], "zip": []}
                else:
                    if 'github' not in config['Custom_Repos']:
                        config['Custom_Repos']['github'] = []
                    if 'zip' not in config['Custom_Repos']:
                        config['Custom_Repos']['zip'] = []
                
                return config
        except Exception as e:
            self.log.error(f"加载配置文件失败: {self.stack_error(e)}。正在重置配置文件...")
            if config_path.exists(): os.remove(config_path)
            await self.gen_config_file()
            self.log.error("配置文件已损坏并被重置。请重启程序。")
            return None

    def get_steam_path(self) -> Path | None:
        try:
            custom_steam_path = self.config.get("Custom_Steam_Path", "").strip()
            if custom_steam_path:
                self.log.debug(f"使用配置中的自定义 Steam 路径: {custom_steam_path}")
                return Path(custom_steam_path)
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
            steam_path, _ = winreg.QueryValueEx(key, 'SteamPath')
            winreg.CloseKey(key)
            return Path(steam_path)
        except Exception:
            self.log.error('获取 Steam 路径失败，请在设置中手动指定。')
            return None
            
    # --- NEW: File Manager Methods ---

    async def _fetch_game_name_for_manager(self, appid: str) -> str:
        """为文件管理器异步获取游戏名称，并使用缓存。现在使用小黑盒API。"""
        if not appid or not appid.isdigit():
            return "无效AppID"
        if appid in self.name_cache:
            return self.name_cache[appid]

        url = f"https://api.xiaoheihe.cn/game/share_game_detail?appid={appid}"
        try:
            response = await self.client.get(url, headers={'User-Agent': 'Cai-Install-Manager/1.0'}, follow_redirects=True)
            response.raise_for_status()

            # 小黑盒接口会 302 到分享页，游戏名在最终 URL 的 title 查询参数里
            name = self._extract_name_from_url(str(response.url))
            if not name:
                title_match = re.search(r'<title>(.*?)</title>', response.text, re.IGNORECASE)
                if title_match:
                    name = title_match.group(1).strip()
                    if " - 小黑盒" in name:
                        name = name.replace(" - 小黑盒", "").strip()

            # 过滤掉小黑盒的站点级占位标题
            if name and name not in ("小黑盒 - 玩家高能聚集地", "小黑盒"):
                self.name_cache[appid] = name
                return name

            # 回退：Steam 官方商店接口
            steam_name = await self._fetch_steam_store_name(appid)
            if steam_name:
                self.name_cache[appid] = steam_name
                return steam_name

            return name or "名称未找到"
        except Exception as e:
            self.log.warning(f"从小黑盒获取 AppID {appid} 的名称失败: {e}")
            try:
                steam_name = await self._fetch_steam_store_name(appid)
                if steam_name:
                    self.name_cache[appid] = steam_name
                    return steam_name
            except Exception:
                pass
            return "获取失败"

    @staticmethod
    def _extract_name_from_url(url: str) -> str | None:
        """从分享页 URL 的 title 参数中还原游戏名。"""
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(url).query)
            raw = (qs.get('title') or [None])[0]
            if raw:
                name = unquote(raw).strip()
                if name and name not in ("小黑盒 - 玩家高能聚集地", "小黑盒"):
                    return name
        except Exception:
            pass
        return None

    async def _fetch_steam_store_name(self, appid: str) -> str | None:
        """回退方案：调用 Steam 官方商店接口取游戏名。"""
        for lang in ('schinese', 'english'):
            try:
                r = await self.client.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={'appids': appid, 'l': lang},
                    headers={'User-Agent': 'Mozilla/5.0'},
                    follow_redirects=True,
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
                node = payload.get(str(appid)) or payload.get(appid)
                if isinstance(node, dict) and node.get('success'):
                    name = (node.get('data') or {}).get('name')
                    if name:
                        return str(name).strip()
            except Exception:
                continue
        return None

    async def _fetch_steam_app_info(self, appid: str) -> dict:
        """调用 Steam 商店接口取 appdetails 的 data 字段，失败返回 {}。"""
        for lang in ('schinese', 'english'):
            try:
                r = await self.client.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={'appids': appid, 'l': lang},
                    headers={'User-Agent': 'Mozilla/5.0'},
                    follow_redirects=True,
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
                node = payload.get(str(appid)) or payload.get(appid)
                if isinstance(node, dict) and node.get('success'):
                    data = node.get('data') or {}
                    if data:
                        return data
            except Exception:
                continue
        return {}

    async def _resolve_cover_url(self, appid: str) -> str:
        """解析游戏封面地址。

        Steam 在 2026 年前后把图片路径从
          cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg
        迁移到
          shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/{hash}/header.jpg
        新上架的游戏在旧路径下完全没有图片（404），必须走商店接口拿带 hash 的新地址。
        这里先查商店接口，拿不到再退回旧路径，保证老游戏也能显示。
        """
        if not appid or not appid.isdigit():
            return ''
        if appid in self.cover_cache:
            return self.cover_cache[appid]

        fallback = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"
        try:
            data = await self._fetch_steam_app_info(appid)
            header_image = (data.get('header_image') or '').strip()
            if header_image:
                # 商店接口给的是 schinese 版本，若取不到可退化到同名英文图
                self.cover_cache[appid] = header_image
                return header_image
        except Exception as e:
            self.log.warning(f"获取 AppID {appid} 的封面图地址失败: {e}")

        self.cover_cache[appid] = fallback
        return fallback
            
    async def get_managed_files(self) -> Dict:
        """扫描所有相关目录，返回文件信息，并批量获取游戏名称。"""
        if not self.steam_path or not self.steam_path.exists():
            return {"error": "Steam路径未配置或无效。"}

        file_data = {"st": [], "gl": [], "os": [], "assistant": []}
        all_appids_to_fetch = set()

        # 1. 扫描文件并收集AppID
        st_path = self.steam_path / 'config' / 'stplug-in'
        gl_path = self.steam_path / 'AppList'
        os_path = self.steam_path / OPENSTEAM_LUA_SUBDIR

        # SteamTools
        if st_path.exists():
            file_data['st'], st_appids = self._scan_st_files(st_path)
            all_appids_to_fetch.update(st_appids)

        # GreenLuma
        if gl_path.exists():
            file_data['gl'], gl_appids = self._scan_generic_files(gl_path, ".txt")
            all_appids_to_fetch.update(gl_appids)

        # OpenSteam (OpenSteamKitten)：lua 语法与 SteamTools 一致，仅目录不同
        if os_path.exists():
            file_data['os'], os_appids = self._scan_st_files(os_path, core_filename="steamtools.lua", core_label="OpenSteam核心文件")
            all_appids_to_fetch.update(os_appids)

        # 2. 批量获取游戏名称
        # 过滤掉已在缓存中的AppID
        appids_to_fetch = [appid for appid in all_appids_to_fetch if appid not in self.name_cache]
        if appids_to_fetch:
            tasks = [self._fetch_game_name_for_manager(appid) for appid in appids_to_fetch]
            results = await asyncio.gather(*tasks)
            for appid, name in zip(appids_to_fetch, results):
                self.name_cache[appid] = name

        # 2.5 批量解析封面图地址（Steam 已迁移到带 hash 的新路径，旧路径对新游戏 404）
        cover_appids = [appid for appid in all_appids_to_fetch if appid and appid.isdigit() and appid not in self.cover_cache]
        if cover_appids:
            cover_results = await asyncio.gather(*[self._resolve_cover_url(appid) for appid in cover_appids])
            for appid, url in zip(cover_appids, cover_results):
                if url:
                    self.cover_cache[appid] = url

        # 3. 将获取到的名称与封面填充回数据
        for category in file_data:
            for item in file_data[category]:
                if item['appid'] in self.name_cache:
                    item['game_name'] = self.name_cache[item['appid']]
                cover = self.cover_cache.get(item['appid'])
                if cover:
                    item['cover_url'] = cover

        return file_data

    def _scan_st_files(self, directory: Path, core_filename: str = "steamtools.lua", core_label: str = "SteamTools核心文件") -> Tuple[List[Dict], set]:
        """扫描SteamTools风格目录（.lua），返回文件数据和AppID集合。
        同时兼容 OpenSteamKitten 的 config/lua 目录（语法相同，仅目录不同）。"""
        data, appids = [], set()
        file_data_map = {}
        try:
            for filename in os.listdir(directory):
                if filename.endswith(".lua") and filename != core_filename:
                    content = (directory / filename).read_text(encoding='utf-8', errors='ignore')
                    match = re.search(r'addappid\s*\(\s*(\d+)', content)
                    appid = match.group(1) if match else "N/A"
                    if appid.isdigit():
                        appids.add(appid)
                        file_data_map[appid] = {"filename": filename, "appid": appid, "game_name": "加载中...", "status": "ok"}
            
            st_lua_path = directory / core_filename
            if st_lua_path.exists():
                data.append({"filename": core_filename, "appid": "N/A", "game_name": core_label, "status": "core_file"})
                content = st_lua_path.read_text(encoding='utf-8', errors='ignore')
                # --- CRITICAL FIX: Use a more general regex to find all appids ---
                unlocked_appids = set(re.findall(r'addappid\s*\(\s*(\d+)', content))
                for appid in unlocked_appids:
                    if appid not in file_data_map:
                        appids.add(appid)
                        file_data_map[appid] = {"filename": f"缺少 {appid}.lua", "appid": appid, "game_name": "加载中...", "status": "unlocked_only"}
        except Exception as e:
            self.log.error(f"扫描SteamTools风格目录失败: {e}")
        
        data.extend(sorted(file_data_map.values(), key=lambda item: int(item.get('appid', 0)), reverse=True))
        return data, appids

    def _scan_generic_files(self, directory: Path, extension: str) -> Tuple[List[Dict], set]:
        """扫描通用目录（如GreenLuma），返回文件数据和AppID集合。"""
        data, appids = [], set()
        try:
            files = [f for f in os.listdir(directory) if f.endswith(extension)]
            for filename in files:
                file_path = directory / filename

                # 对于GreenLuma，读取TXT文件内容获取AppID
                if extension == ".txt":
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read().strip()
                            # 尝试将文件内容解析为AppID
                            if content.isdigit():
                                appid = content
                            else:
                                # 如果内容不是纯数字，使用文件名作为备用
                                appid = Path(filename).stem
                    except Exception as e:
                        self.log.warning(f"读取GreenLuma文件 {filename} 失败: {e}")
                        appid = Path(filename).stem
                else:
                    # 对于其他文件类型，使用文件名
                    appid = Path(filename).stem

                if appid.isdigit():
                    appids.add(appid)
                    data.append({"filename": filename, "appid": appid, "game_name": "加载中...", "status": "ok"})
        except Exception as e:
            self.log.error(f"扫描目录 {directory} 失败: {e}")

        # 按AppID数字大小排序
        data.sort(key=lambda x: int(x.get('appid', 0)) if x.get('appid', '0').isdigit() else 0, reverse=True)
        return data, appids
    
    def delete_managed_files(self, file_type: str, items: List[Dict]) -> Dict:
        """根据类型和项目列表删除文件, 并清理关联的manifest。"""
        if not self.steam_path or not self.steam_path.exists():
            return {"success": False, "message": "Steam路径无效。"}
        
        base_path = None
        if file_type == 'st':
            base_path = self.steam_path / 'config' / 'stplug-in'
        elif file_type == 'gl':
            base_path = self.steam_path / 'AppList'
        elif file_type == 'os':
            base_path = self.steam_path / OPENSTEAM_LUA_SUBDIR
        
        if not base_path:
            return {"success": False, "message": f"未知的类型: {file_type}。"}

        deleted_count, failed, manifests_deleted_count = 0, [], 0
        
        for item in items:
            try:
                # 步骤1: 如果是ST/OS类型，清理核心lua中的解锁条目
                if file_type in ('st', 'os') and item.get('status') != 'core_file' and item.get('appid', 'N/A').isdigit():
                    if file_type == 'st':
                        self._modify_st_lua_for_delete(item['appid'])
                    else:
                        self._modify_st_lua_for_delete(item['appid'], lua_dir=self.steam_path / OPENSTEAM_LUA_SUBDIR)

                # 步骤2: 清理物理文件和关联的 manifest
                filename = item.get('filename')
                if filename and "缺少" not in filename:
                    file_path = base_path / filename
                    if file_path.exists() and file_path.is_file():
                        # --- NEW: Logic to clean up associated manifest files ---
                        if file_type in ('st', 'os') and filename.endswith('.lua'):
                            try:
                                content = file_path.read_text(encoding='utf-8', errors='ignore')
                                gids = re.findall(r'setManifestid\s*\(\s*\d+\s*,\s*"(\d+)"\s*\)', content)
                                depotcache_paths = [
                                    self.steam_path / 'depotcache',
                                    self.steam_path / 'config' / 'depotcache'
                                ]
                                for gid in gids:
                                    for cache_path in depotcache_paths:
                                        if cache_path.exists():
                                            for mf in cache_path.glob(f'*_{gid}.manifest'):
                                                if mf.exists():
                                                    os.remove(mf)
                                                    manifests_deleted_count += 1
                            except Exception as e:
                                self.log.error(f"清理 {filename} 的清单时失败: {e}")
                        
                        # 删除主文件
                        os.remove(file_path)
                        deleted_count += 1
            except Exception as e:
                failed.append(f"{item.get('filename', item.get('appid'))}: {e}")

        message = f"成功处理 {len(items) - len(failed)}/{len(items)} 个条目。"
        if deleted_count > 0:
            message += f" 删除了 {deleted_count} 个文件。"
        if manifests_deleted_count > 0:
             message += f" 清理了 {manifests_deleted_count} 个关联清单文件。"
        if failed:
            message += f" 失败条目: {', '.join(failed)}"
        
        return {"success": not failed, "message": message}


    def _modify_st_lua_for_delete(self, appid: str, lua_dir: Path = None, core_filename: str = "steamtools.lua"):
        """从核心lua（默认 steamtools.lua）中移除一个解锁条目。
        lua_dir 用于指定目标目录（如 OpenSteamKitten 的 config/lua）。"""
        if lua_dir is None:
            lua_dir = self.steam_path / 'config' / 'stplug-in'
        st_lua_path = lua_dir / core_filename
        if not st_lua_path.exists(): return

        try:
            content = st_lua_path.read_text(encoding='utf-8', errors='ignore')
            # 匹配 addappid(XXX, 1) 或 addappid(XXX) 两种形式
            pattern = re.compile(r'^\s*addappid\s*\(\s*' + re.escape(appid) + r'[^)]*\)\s*$', re.MULTILINE)
            new_content, count = pattern.subn('', content)
            
            if count > 0:
                # 清理空行
                new_content_cleaned = "\n".join(line for line in new_content.splitlines() if line.strip())
                st_lua_path.write_text(new_content_cleaned + "\n" if new_content_cleaned else "", encoding='utf-8')
                self.log.info(f"已从 {st_lua_path} 移除 AppID {appid} 的解锁条目。")
        except Exception as e:
            self.log.error(f"修改 {st_lua_path} 以删除 AppID {appid} 时失败: {e}")
            raise # 重新抛出异常，让上层捕获

    def _get_manager_dir(self, file_type: str) -> Path | None:
        """返回入库管理器对应类型的目录。"""
        if file_type == 'st':
            return self.steam_path / 'config' / 'stplug-in'
        elif file_type == 'gl':
            return self.steam_path / 'AppList'
        elif file_type == 'os':
            return self.steam_path / OPENSTEAM_LUA_SUBDIR
        return None

    def get_file_content(self, file_type: str, filename: str) -> Dict:
        """读取入库管理器中某个文件的文本内容（用于编辑器）。"""
        directory = self._get_manager_dir(file_type)
        if not directory:
            return {"success": False, "message": f"未知的类型: {file_type}。"}
        # 防路径穿越
        name = Path(filename).name
        file_path = directory / name
        if not file_path.exists() or not file_path.is_file():
            return {"success": False, "message": f"文件不存在: {name}"}
        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            return {"success": True, "content": content, "filename": name}
        except Exception as e:
            return {"success": False, "message": f"读取文件失败: {e}"}

    def save_file_content(self, file_type: str, filename: str, content: str) -> Dict:
        """保存入库管理器中某个文件的文本内容（用于编辑器）。"""
        directory = self._get_manager_dir(file_type)
        if not directory:
            return {"success": False, "message": f"未知的类型: {file_type}。"}
        name = Path(filename).name
        if not name or name.startswith('.') or '/' in name or '\\' in name:
            return {"success": False, "message": "无效的文件名。"}
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / name
        try:
            file_path.write_text(content, encoding='utf-8')
            return {"success": True, "message": f"已保存 {name}。", "filename": name}
        except Exception as e:
            return {"success": False, "message": f"保存文件失败: {e}"}

    async def force_unlock(self, appid: str, action: str = "add") -> Dict:
        """在入库管理器中强制添加/移除某个 AppID 的解锁条目。

        action='add'   -> 写入 <appid>.lua 并加入核心 lua（若存在）
        action='remove'-> 删除 <appid>.lua 并从核心 lua 移除
        """
        if not appid or not appid.isdigit():
            return {"success": False, "message": "无效的 AppID。"}
        if action not in ("add", "remove"):
            return {"success": False, "message": f"未知的操作: {action}。"}

        # 每个请求都会新建 backend 实例，故这里按需初始化，确保 unlocker_type / steam_path 就绪
        if not self.unlocker_type or not self.steam_path:
            await self.initialize()
        if not self.steam_path:
            return {"success": False, "message": "Steam 路径未配置或无效。"}

        lua_dir = self._lua_write_dir(self.unlocker_type or "steamtools")
        lua_dir.mkdir(parents=True, exist_ok=True)
        lua_file = lua_dir / f"{appid}.lua"

        if action == "add":
            # 主游戏 appid
            lua_file.write_text(f'addappid({appid})\n', encoding='utf-8')
            # 同时把 appid 加入核心 lua（若存在），保证 SteamTools/OpenSteam 都能识别
            self._add_appid_to_core_lua(appid, lua_dir)
            self.log.info(f"已强制解锁 AppID {appid} -> {lua_file}")
            return {"success": True, "message": f"已强制解锁 AppID {appid}。"}
        else:
            removed_files = []
            if lua_file.exists():
                os.remove(lua_file)
                removed_files.append(lua_file.name)
            # 从核心 lua 中移除
            try:
                self._modify_st_lua_for_delete(appid, lua_dir=lua_dir)
                removed_files.append("核心lua条目")
            except Exception:
                pass
            self.log.info(f"已移除 AppID {appid} 的解锁 ({', '.join(removed_files) or '无文件'})")
            return {"success": True, "message": f"已移除 AppID {appid} 的解锁。"}

    def _add_appid_to_core_lua(self, appid: str, lua_dir: Path):
        """把 appid 追加到核心 lua（steamtools.lua）中（若该文件存在）。"""
        core_lua = lua_dir / "steamtools.lua"
        if not core_lua.exists():
            return
        try:
            content = core_lua.read_text(encoding='utf-8', errors='ignore')
            if re.search(r'^\s*addappid\s*\(\s*' + re.escape(appid) + r'\s*\)\s*$', content, re.MULTILINE):
                return
            with open(core_lua, 'a', encoding='utf-8') as f:
                f.write(f'addappid({appid})\n')
            self.log.info(f"已将 AppID {appid} 追加到核心 lua。")
        except Exception as e:
            self.log.warning(f"追加 AppID {appid} 到核心 lua 失败: {e}")

    # --- END OF File Manager Methods ---

    # --- NEW: Custom repository support functions ---
    def get_custom_github_repos(self) -> List[Dict]:
        """获取自定义GitHub仓库列表"""
        custom_repos = self.config.get("Custom_Repos", {}).get("github", [])
        validated_repos = []
        
        for repo in custom_repos:
            if isinstance(repo, dict) and 'name' in repo and 'repo' in repo:
                validated_repos.append(repo)
            else:
                self.log.warning(f"无效的自定义GitHub仓库配置: {repo}")
        
        return validated_repos

    def get_custom_zip_repos(self) -> List[Dict]:
        """获取自定义ZIP仓库列表"""
        custom_repos = self.config.get("Custom_Repos", {}).get("zip", [])
        validated_repos = []
        
        for repo in custom_repos:
            if isinstance(repo, dict) and 'name' in repo and 'url' in repo:
                # 验证URL中是否包含{app_id}占位符
                if '{app_id}' in repo['url']:
                    validated_repos.append(repo)
                else:
                    self.log.warning(f"自定义ZIP仓库URL缺少{app_id}占位符: {repo}")
            else:
                self.log.warning(f"无效的自定义ZIP仓库配置: {repo}")
        
        return validated_repos

    async def process_custom_zip_manifest(self, app_id: str, repo_config: Dict, add_all_dlc: bool = False, patch_depot_key: bool = False) -> bool:
        """处理自定义ZIP清单库"""
        repo_name = repo_config.get('name', '未知仓库')
        url_template = repo_config.get('url', '')
        
        # 替换占位符
        download_url = url_template.replace('{app_id}', app_id)
        
        return await self._process_zip_manifest_generic(app_id, download_url, f"自定义ZIP库 ({repo_name})", self.unlocker_type, False, add_all_dlc, patch_depot_key)

    def get_all_github_repos(self) -> List[str]:
        """获取所有GitHub仓库（内置+自定义）

        备注：`manilua-hub/ManifestHub3`、`steamtoolsapp/ManifestHub`、`ooiiuur/ManifestHubSharing`
        实测与 `steamtools-games/ManifestHub3` 是同一数据库的克隆（同 appid 的 codeload zip
        内容 sha256 一致，仅 zip 时间戳字节不同），不提供额外覆盖，故不列入避免无谓请求。
        `dc190/ManifestHub` 分支数少（271）但与 ManifestHub3 有互补（如 2928600），保留。
        """
        builtin_repos = [
            'Auiowu/ManifestAutoUpdate',
            'SteamAutoCracks/ManifestHub',
            # 6.2 万 appid 分支的超大清单库，覆盖 Auiowu 没有的冷门 / 新作 / 成人游戏
            'steamtools-games/ManifestHub3',
            # 271 分支的独立 fork，与上面的分支集合互补
            'dc190/ManifestHub',
        ]
        custom_repos = [repo['repo'] for repo in self.get_custom_github_repos()]
        return builtin_repos + custom_repos

    # NEW: HTTP helper function for safe requests with retry mechanism
    async def http_get_safe(self, url: str, timeout: int = 30, max_retries: int = 3, retry_delay: float = 1.0) -> httpx.Response | None:
        """安全的HTTP GET请求，带错误处理和重试机制"""
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                # Use different timeout strategies for different attempts
                current_timeout = timeout if attempt == 0 else min(timeout * (attempt + 1), 60)
                
                response = await self.client.get(url, timeout=current_timeout)
                if response.status_code == 200:
                    if attempt > 0:  # Log successful retry
                        self.log.info(f"HTTP请求在第 {attempt + 1} 次尝试后成功: {url}")
                    return response
                else:
                    self.log.warning(f"HTTP请求失败，状态码: {response.status_code} - {url} (尝试 {attempt + 1}/{max_retries})")
                    if response.status_code in [429, 503, 502, 504]:  # Retry on server errors
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay * (attempt + 1))
                            continue
                    return None
                    
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as e:
                last_exception = e
                self.log.warning(f"HTTP请求超时: {url} (尝试 {attempt + 1}/{max_retries}) - {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                    
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exception = e
                self.log.warning(f"HTTP连接错误: {url} (尝试 {attempt + 1}/{max_retries}) - {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                    
            except Exception as e:
                last_exception = e
                self.log.error(f"HTTP请求异常: {url} (尝试 {attempt + 1}/{max_retries}) - {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                break
        
        self.log.error(f"HTTP请求在 {max_retries} 次尝试后仍然失败: {url} - 最后异常: {last_exception}")
        return None

    # NEW: Updated DLC retrieval function with better error handling
    async def get_dlc_ids_safe(self, appid: str) -> List[str]:
        """安全的DLC ID获取函数，支持多数据源回退 (ddxnb -> steamcmd -> steam store)"""
        self.log.info(f"正在获取 AppID {appid} 的DLC信息...")

        # 通用解析函数 (ddxnb 和 steamcmd 结构一致)
        def parse_steamcmd_style_json(json_data: dict) -> List[str]:
            try:
                info = json_data.get("data", {}).get(str(appid), {})
                # 尝试不同的字段位置，兼顾 info 和 extended 信息
                dlc_str = info.get("extended", {}).get("listofdlc", "") or info.get("common", {}).get("listofdlc", "")
                if dlc_str:
                    return sorted(filter(str.isdigit, map(str.strip, dlc_str.split(","))), key=int)
            except Exception:
                pass
            return []

        # 1. 尝试 ddxnb 源 (国内优化)
        self.log.debug(f"尝试从 ddxnb源 获取 AppID {appid} 的DLC...")
        data = await self.http_get_safe(f"https://steam.ddxnb.cn/v1/info/{appid}", timeout=20, max_retries=2)
        if data:
            try:
                dlc_ids = parse_steamcmd_style_json(data.json())
                if dlc_ids:
                    self.log.info(f"从 ddxnb源 成功获取到 {len(dlc_ids)} 个DLC")
                    return dlc_ids
                self.log.debug(f"ddxnb源 中 AppID {appid} 没有DLC信息或解析为空")
            except Exception as e:
                self.log.warning(f"解析 ddxnb源 响应失败: {e}")
        else:
            self.log.warning(f"无法从 ddxnb源 获取 AppID {appid} 的数据")

        # 2. 尝试 SteamCMD API (原源)
        self.log.debug(f"尝试从 SteamCMD API 获取 AppID {appid} 的DLC...")
        data = await self.http_get_safe(f"https://api.steamcmd.net/v1/info/{appid}", timeout=20, max_retries=2)
        if data:
            try:
                dlc_ids = parse_steamcmd_style_json(data.json())
                if dlc_ids:
                    self.log.info(f"从 SteamCMD API 成功获取到 {len(dlc_ids)} 个DLC")
                    return dlc_ids
                self.log.debug(f"SteamCMD API 中 AppID {appid} 没有DLC信息")
            except Exception as e:
                self.log.warning(f"解析 SteamCMD API 响应失败: {e}")
        else:
            self.log.warning(f"无法从 SteamCMD API 获取 AppID {appid} 的数据")
        
        # 3. 降级：使用官方 API (兜底)
        self.log.debug(f"尝试从 Steam 官方 API 获取 AppID {appid} 的DLC...")
        api_variants = [
            f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese",
            f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english",
            f"https://store.steampowered.com/api/appdetails?appids={appid}"
        ]
        
        for api_url in api_variants:
            data = await self.http_get_safe(api_url, timeout=25, max_retries=2, retry_delay=2.0)
            if data:
                try:
                    j = data.json()
                    app_data = j.get(str(appid), {})
                    if app_data.get("success") and "data" in app_data:
                        dlc_list = app_data["data"].get("dlc", [])
                        if dlc_list:
                            dlc_ids = [str(d) for d in dlc_list]
                            self.log.info(f"从 Steam 官方 API 成功获取到 {len(dlc_ids)} 个DLC")
                            return dlc_ids
                except Exception as e:
                    self.log.warning(f"解析 Steam 官方 API 响应失败 ({api_url}): {e}")
                    continue
        
        self.log.info(f"未找到 AppID {appid} 的DLC信息（已尝试所有数据源）")
        return []

    # NEW: Updated depot retrieval function with better error handling
    async def get_depots_safe(self, appid: str) -> List[Tuple[str, str, int, str]]:
        """安全的Depot获取函数，返回 (depot_id, manifest_id, size, source) 元组列表"""
        self.log.info(f"正在获取 AppID {appid} 的Depot信息...")
        
        # 通用解析函数 (适用于 ddxnb 和 steamcmd)
        def parse_steamcmd_style_depots(json_data: dict) -> List[Tuple[str, str, int, str]]:
            out = []
            try:
                info = json_data.get("data", {}).get(str(appid), {})
                depots = info.get("depots", {})
                if depots:
                    for depot_id, depot_info in depots.items():
                        if not isinstance(depot_info, dict): continue
                        manifest_info = depot_info.get("manifests", {}).get("public")
                        if not isinstance(manifest_info, dict): continue
                        
                        manifest_id = manifest_info.get("gid")
                        size = int(manifest_info.get("download", 0))
                        dlc_appid = depot_info.get("dlcappid")
                        source_label = f"DLC:{dlc_appid}" if dlc_appid else "主游戏"
                        
                        if manifest_id:
                            out.append((depot_id, manifest_id, size, source_label))
            except Exception:
                pass
            return out

        # 1. 尝试 ddxnb 源 (国内优化)
        self.log.debug(f"尝试从 ddxnb源 获取 AppID {appid} 的Depot...")
        data = await self.http_get_safe(f"https://steam.ddxnb.cn/v1/info/{appid}", timeout=20, max_retries=2)
        if data:
            try:
                out = parse_steamcmd_style_depots(data.json())
                if out:
                    self.log.info(f"从 ddxnb源 成功获取到 {len(out)} 个Depot")
                    return out
                if "data" in data.json():
                     self.log.debug(f"ddxnb源 返回数据中无Depot信息")
            except Exception as e:
                self.log.warning(f"解析 ddxnb源 Depot 信息失败: {e}")
        else:
            self.log.warning(f"无法从 ddxnb源 获取 AppID {appid} 的Depot数据")

        # 2. 尝试 SteamCMD API (原源)
        self.log.debug(f"尝试从 SteamCMD API 获取 AppID {appid} 的Depot...")
        data = await self.http_get_safe(f"https://api.steamcmd.net/v1/info/{appid}", timeout=20, max_retries=2)
        if data:
            try:
                out = parse_steamcmd_style_depots(data.json())
                if out:
                    self.log.info(f"从 SteamCMD API 成功获取到 {len(out)} 个Depot")
                    return out
            except Exception as e:
                self.log.warning(f"解析 SteamCMD API Depot 信息失败: {e}")
        else:
            self.log.warning(f"无法从 SteamCMD API 获取 AppID {appid} 的Depot数据")
        
        # 3. 降级：使用官方 API (兜底)
        self.log.debug(f"尝试从 Steam 官方 API 获取 AppID {appid} 的Depot...")
        api_variants = [
            f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese",
            f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english",
            f"https://store.steampowered.com/api/appdetails?appids={appid}"
        ]
        
        for api_url in api_variants:
            data = await self.http_get_safe(api_url, timeout=25, max_retries=2, retry_delay=2.0)
            if data:
                try:
                    j = data.json()
                    app_data = j.get(str(appid), {})
                    if app_data.get("success") and "data" in app_data:
                        depots = app_data["data"].get("depots", {})
                        out = []
                        for depot_id, depot_info in depots.items():
                            if not isinstance(depot_info, dict): continue
                            manifest_info = depot_info.get("manifests", {}).get("public")
                            if not isinstance(manifest_info, dict): continue
                            
                            manifest_id = manifest_info.get("gid")
                            size = int(manifest_info.get("download", 0))
                            dlc_appid = depot_info.get("dlcappid")
                            source_label = f"DLC:{dlc_appid}" if dlc_appid else "主游戏"
                            
                            if manifest_id:
                                out.append((depot_id, manifest_id, size, source_label))
                        if out:
                            self.log.info(f"从 Steam 官方 API 成功获取到 {len(out)} 个Depot")
                            return out
                except Exception as e:
                    self.log.warning(f"解析 Steam 官方 API Depot 信息失败 ({api_url}): {e}")
                    continue
        
        self.log.info(f"未找到 AppID {appid} 的Depot信息（已尝试所有数据源）")
        return []

    # Workshop-related methods
    def extract_workshop_id(self, input_text: str) -> str | None:
        """Extract workshop ID from URL or direct ID input"""
        input_text = input_text.strip()
        if not input_text:
            return None
        
        # Try to match URL pattern
        url_match = re.search(r"https?://steamcommunity\.com/sharedfiles/filedetails/\?id=(\d+)", input_text)
        if url_match:
            return url_match.group(1)
        
        # If it's just digits, treat as direct ID
        if input_text.isdigit():
            return input_text
        
        return None


    async def get_workshop_depot_info(self, workshop_id: str) -> Tuple[str, str, str] | None:
        """Get depot and manifest info for workshop item with title"""
        try:
            self.log.info(f"正在查询创意工坊物品 {workshop_id} 的信息...")
            api_url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
            data = {
                'itemcount': 1,
                'publishedfileids[0]': workshop_id
            }
            
            max_retries, retry_delay = 3, 2
            for attempt in range(max_retries):
                try:
                    response = await self.client.post(api_url, data=data, timeout=30)
                    response.raise_for_status()
                    
                    result = response.json()
                    
                    if 'response' not in result or 'publishedfiledetails' not in result['response'] or not result['response']['publishedfiledetails']:
                        self.log.error("API响应格式不正确或未找到物品详情")
                        return None
                    
                    details = result['response']['publishedfiledetails'][0]
                    
                    if int(details.get('result', 0)) != 1:
                        self.log.error(f"未找到创意工坊物品 {workshop_id}")
                        return None
                    
                    consumer_app_id = details.get('consumer_app_id')
                    hcontent_file = details.get('hcontent_file')
                    title = details.get('title', '未知标题')
                    
                    if not consumer_app_id or not hcontent_file:
                        self.log.error(f"创意工坊物品 '{title}' 缺少必要的信息 (App ID 或 Manifest ID)。")
                        return None
                    
                    self.log.info(f"成功获取创意工坊物品信息:")
                    self.log.info(f"  标题: {title}")
                    self.log.info(f"  所属游戏 AppID: {consumer_app_id}")
                    self.log.info(f"  清单 ManifestID: {hcontent_file}")
                    return str(consumer_app_id), str(hcontent_file), title
                    
                except httpx.RequestError as e:
                    if attempt < max_retries - 1:
                        self.log.warning(f"API请求失败，正在重试 ({attempt+1}/{max_retries})...")
                        await asyncio.sleep(retry_delay)
                    else:
                        self.log.error(f"API请求失败: {e}")
                        return None
                except Exception as e:
                    self.log.error(f"获取创意工坊物品信息出错: {self.stack_error(e)}")
                    return None
                    
        except Exception as e:
            self.log.error(f"获取创意工坊物品信息时发生错误: {self.stack_error(e)}")
            return None


    async def download_workshop_manifest(self, depot_id: str, manifest_id: str) -> bytes | None:
        """Download workshop manifest using new method from CLI version"""
        output_filename = f"{depot_id}_{manifest_id}.manifest"
        self.log.info(f"准备下载清单: {output_filename}")
        
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # GET /api/download_manifest 直接返回 raw manifest，无需 session token。
                self.log.info(f"正在请求清单: {output_filename} [Depot: {depot_id}, Manifest: {manifest_id}]")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://manifest.steam.run/",
                    "Accept": "application/octet-stream, */*",
                }

                # 等待避免频率限制
                await asyncio.sleep(2)

                manifest_response = await self.client.get(
                    "https://manifest.steam.run/api/download_manifest",
                    params={"depot_id": str(depot_id), "manifest_id": str(manifest_id)},
                    headers=headers,
                    timeout=180,
                    follow_redirects=True,
                )

                if manifest_response.status_code == 429:
                    self.log.warning(f"请求频率过高，等待后重试...")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(30)
                        continue
                    return None

                if manifest_response.status_code != 200:
                    self.log.error(f"下载失败，状态码: {manifest_response.status_code}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(10)
                        continue
                    return None

                manifest_content = manifest_response.content

                # Step 4: 处理文件内容（检查是否为ZIP）
                final_content = None
                
                # 检查是否为ZIP文件
                if manifest_content.startswith(b'PK\x03\x04'):
                    self.log.info("检测到ZIP文件，正在自动解压...")
                    try:
                        with io.BytesIO(manifest_content) as mem_zip:
                            with zipfile.ZipFile(mem_zip, 'r') as z:
                                file_list = z.namelist()
                                if len(file_list) == 1:
                                    target_file = file_list[0]
                                    self.log.info(f"从ZIP中提取文件: {target_file}")
                                    final_content = z.read(target_file)
                                else:
                                    self.log.warning(f"ZIP包中文件数量不为1: {len(file_list)}")
                                    final_content = manifest_content
                    except Exception as e:
                        self.log.warning(f"处理ZIP文件时出错: {e}")
                        final_content = manifest_content
                else:
                    self.log.info("文件不是ZIP，将直接保存。")
                    final_content = manifest_content
                
                if not final_content:
                    self.log.error("最终文件内容为空")
                    if attempt < max_retries - 1:
                        continue
                    return None
                
                self.log.info(f"成功下载创意工坊清单，大小: {len(final_content)} 字节")
                return final_content
                
            except Exception as e:
                self.log.error(f"下载过程中出错: {e}")
                if attempt < max_retries - 1:
                    self.log.info(f"等待后重试... (尝试 {attempt + 2}/{max_retries})")
                    await asyncio.sleep(15)
                    continue
        
        self.log.error(f"下载清单 {output_filename} 失败：所有重试都失败了")
        return None

    async def process_workshop_item(self, workshop_input: str, copy_to_config: bool = True, copy_to_depot: bool = True) -> bool:
        """Process workshop item and copy manifest to specified directories"""
        workshop_id = self.extract_workshop_id(workshop_input)
        if not workshop_id:
            self.log.error(f"无法从输入中提取有效的创意工坊ID: {workshop_input}")
            return False
            
        # Get depot and manifest info
        details = await self.get_workshop_depot_info(workshop_id)
        if not details:
            return False # 错误已在 get_workshop_depot_info 中记录
        
        consumer_app_id, hcontent_file, title = details
        
        # Download manifest using new method
        manifest_content = await self.download_workshop_manifest(consumer_app_id, hcontent_file)
        if not manifest_content:
            return False
        
        # Generate filename
        output_filename = f"{consumer_app_id}_{hcontent_file}.manifest"
        
        try:
            # Copy to specified directories
            success_count = 0
            
            if copy_to_config:
                config_depot_path = self.steam_path / 'config' / 'depotcache'
                config_depot_path.mkdir(parents=True, exist_ok=True)
                config_file_path = config_depot_path / output_filename
                async with aiofiles.open(config_file_path, 'wb') as f:
                    await f.write(manifest_content)
                self.log.info(f"清单文件已保存到: {config_file_path}")
                success_count += 1
            
            if copy_to_depot:
                depot_cache_path = self.steam_path / 'depotcache'
                depot_cache_path.mkdir(parents=True, exist_ok=True)
                depot_file_path = depot_cache_path / output_filename
                async with aiofiles.open(depot_file_path, 'wb') as f:
                    await f.write(manifest_content)
                self.log.info(f"清单文件已保存到: {depot_file_path}")
                success_count += 1
            
            if success_count > 0:
                self.log.info(f"创意工坊清单 {output_filename} 处理完成。标题: {title}")
                return True
            else:
                self.log.error("未指定任何目标目录。")
                return False
                
        except Exception as e:
            self.log.error(f"保存创意工坊清单文件时出错: {self.stack_error(e)}")
            return False

    async def _download_manifest_buqiuren(self, depot_id: str, manifest_id: str, depot_name: str) -> bool:
        """使用不求人接口下载清单"""
        output_filename = f"{depot_id}_{manifest_id}.manifest"
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # GET /api/download_manifest?depot_id=..&manifest_id=.. 返回 raw manifest。
                self.log.info(f"正在请求清单: {output_filename} [Depot: {depot_id}, Manifest: {manifest_id}]")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://manifest.steam.run/",
                    "Accept": "application/octet-stream, */*",
                }

                # 轻微随机间隔，避免触发站点限流
                await asyncio.sleep(random.uniform(1, 3))

                manifest_response = await self.client.get(
                    "https://manifest.steam.run/api/download_manifest",
                    params={"depot_id": str(depot_id), "manifest_id": str(manifest_id)},
                    headers=headers,
                    timeout=180,
                    follow_redirects=True,
                )

                if manifest_response.status_code == 429:
                    self.log.warning("请求频率过高，等待后重试...")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(30)
                        continue
                    return False

                if manifest_response.status_code != 200:
                    self.log.error(f"下载失败，状态码: {manifest_response.status_code}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(10)
                        continue
                    return False

                manifest_content = manifest_response.content
                
                # 处理文件内容（检查是否为ZIP）
                final_content = None
                
                if manifest_content.startswith(b'PK\x03\x04'):
                    self.log.info("检测到ZIP文件，正在自动解压...")
                    try:
                        with io.BytesIO(manifest_content) as mem_zip:
                            with zipfile.ZipFile(mem_zip, 'r') as z:
                                file_list = z.namelist()
                                if len(file_list) == 1:
                                    target_file = file_list[0]
                                    self.log.info(f"从ZIP中提取文件: {target_file}")
                                    final_content = z.read(target_file)
                                else:
                                    self.log.warning(f"ZIP包中文件数量不为1: {len(file_list)}")
                                    final_content = manifest_content
                    except Exception as e:
                        self.log.warning(f"处理ZIP文件时出错: {e}")
                        final_content = manifest_content
                else:
                    final_content = manifest_content
                
                if not final_content:
                    self.log.error("最终文件内容为空")
                    if attempt < max_retries - 1:
                        continue
                    return False
                
                # 保存文件到depotcache目录
                if self._is_lua_unlocker(self.unlocker_type):
                    st_depot_path = self.steam_path / 'config' / 'depotcache'
                    gl_depot_path = self.steam_path / 'depotcache'
                    
                    st_depot_path.mkdir(parents=True, exist_ok=True)
                    gl_depot_path.mkdir(parents=True, exist_ok=True)
                    
                    (st_depot_path / output_filename).write_bytes(final_content)
                    self.log.info(f"清单已保存到: {st_depot_path / output_filename}")
                    
                    (gl_depot_path / output_filename).write_bytes(final_content)
                    self.log.info(f"清单已保存到: {gl_depot_path / output_filename}")
                else:
                    # GreenLuma
                    depot_path = self.steam_path / 'depotcache'
                    depot_path.mkdir(parents=True, exist_ok=True)
                    (depot_path / output_filename).write_bytes(final_content)
                    self.log.info(f"清单已保存到: {depot_path / output_filename}")
                
                self.log.info(f"成功下载清单: {depot_name} ({output_filename})")
                return True
                
            except Exception as e:
                self.log.error(f"下载过程中出错: {e}")
                if attempt < max_retries - 1:
                    self.log.info(f"等待后重试... (尝试 {attempt + 2}/{max_retries})")
                    await asyncio.sleep(15)
                    continue
        
        self.log.error(f"下载清单 {output_filename} 失败：所有重试都失败了")
        return False

    async def _get_cached_sudama_data(self) -> Dict:
        """
        核心函数：获取 Sudama 密钥数据
        逻辑：检查本地 sudama_cache.json -> 检查时间戳是否超过24小时 -> 下载或读取缓存
        """
        cache_file = self.project_root / "sudama_cache.json"
        current_time = time.time()
        one_day_seconds = 86400

        # 1. 尝试读取缓存
        if cache_file.exists():
            try:
                async with aiofiles.open(cache_file, 'r', encoding='utf-8') as f:
                    cache_content = json.loads(await f.read())
                
                last_update = cache_content.get('timestamp', 0)
                # 检查是否过期
                if current_time - last_update < one_day_seconds:
                    self.log.info(f"使用本地缓存的密钥库 (上次更新: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_update))})")
                    return cache_content.get('data', {})
                else:
                    self.log.info("本地密钥缓存已超过24小时，准备从服务器同步最新数据...")
            except Exception as e:
                self.log.warning(f"读取本地缓存失败，将重新下载: {e}")

        # 2. 下载新数据（按顺序尝试多个可用数据源，任一成功即用）
        candidate_urls = [
            ("Sudama API", "https://api.993499094.xyz/depotkeys.json"),
            # SteamAutoCracks/ManifestHub 的 depotkeys.json：纯 raw 通道，不消耗 GitHub API 配额，
            # 28 万条 depot 密钥，是 Sudama 的主备源。
            ("SteamAutoCracks/ManifestHub", "https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/main/depotkeys.json"),
        ]
        data = None
        used_source = None
        for src_name, url in candidate_urls:
            try:
                self.log.info(f"正在从 {src_name} 下载全量密钥库...")
                # 数据量较大，使用较大的超时
                response = await self.client.get(url, timeout=180)
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, dict) or not parsed:
                    self.log.warning(f"{src_name} 返回的数据格式不正确或为空，尝试下一个数据源。")
                    continue
                data = parsed
                used_source = src_name
                break
            except Exception as e:
                self.log.warning(f"从 {src_name} 下载密钥库失败: {type(e).__name__}: {e}")

        if data is None:
            self.log.error("所有密钥库数据源均下载失败。")
            # 下载失败时的兜底：如果有过期的缓存，尽量使用过期的
            if cache_file.exists():
                try:
                    self.log.warning("网络获取失败，尝试使用旧的本地缓存...")
                    async with aiofiles.open(cache_file, 'r', encoding='utf-8') as f:
                        return json.loads(await f.read()).get('data', {})
                except:
                    pass
            return {}

        # 3. 写入缓存
        try:
            cache_data = {
                "timestamp": current_time,
                "data": data
            }
            async with aiofiles.open(cache_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(cache_data, ensure_ascii=False))
            self.log.info(f"密钥库下载完成并缓存（来源: {used_source}），共 {len(data)} 条数据。")
        except Exception as e:
            self.log.warning(f"写入密钥库缓存失败（不影响本次使用）: {e}")
        return data


# --- SUDAMA REPO START ---
    async def _get_sudama_data(self) -> Dict[str, str]:
        """从 sudama API 获取所有密钥数据 (兼容性包装)"""
        return await self._get_cached_sudama_data()

    async def process_sudama_manifest(self, app_id: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool = False, patch_depot_key: bool = False) -> bool:
        """处理 Sudama 库清单"""
        try:
            self.log.info(f'正从 Sudama 库处理 AppID {app_id} 的清单...')
            
            # 1. 获取 Depot 和 Manifest 信息 (使用现有的 SteamUI/DDXNB 接口)
            depot_manifest_map = await self._get_depots_and_manifests_from_steamui(app_id)
            if not depot_manifest_map:
                self.log.error(f"未能从 API 获取到 AppID {app_id} 的 depot 信息")
                return False
            
            self.log.info(f"获取到 {len(depot_manifest_map)} 个 depot 及其 manifest")

            # 2. 获取 Sudama 的所有密钥数据（带缓存）
            sudama_keys = await self._get_cached_sudama_data()
            if not sudama_keys:
                self.log.error("无法获取 Sudama 密钥库数据")
                return False

            # 3. 匹配 Depot 与 Key
            valid_depots = {}
            for depot_id in depot_manifest_map.keys():
                if depot_id in sudama_keys:
                    key = sudama_keys[depot_id]
                    if key and str(key).strip():
                        valid_depots[depot_id] = str(key).strip()
                        self.log.info(f"在 Sudama 库中找到 depot {depot_id} 的密钥")
                else:
                    self.log.warning(f"Sudama 库中未找到 depot {depot_id} 的密钥")

            if not valid_depots:
                self.log.warning(f"AppID {app_id} 没有在 Sudama 库中找到任何有效的 depot 密钥")
                return False

            # 4. 根据解锁工具类型处理 (复用 ManifestHub V2 的逻辑)
            if self._is_lua_unlocker(unlocker_type):
                # 将 sudama_keys 作为 depotkeys_data 传入，以便复用修补逻辑
                return await self._process_steamautocracks_v2_for_steamtools(
                    app_id, valid_depots, depot_manifest_map, use_st_auto_update, add_all_dlc, patch_depot_key, sudama_keys, unlocker_type
                )
            else:
                return await self._process_steamautocracks_v2_for_greenluma(app_id, valid_depots)

        except Exception as e:
            self.log.error(f'处理 Sudama 库清单时出错: {self.stack_error(e)}')
            return False
            
            
    async def process_buqiuren_manifest(self, app_id: str, unlocker_type: str = "", use_st_auto_update: bool = False, add_all_dlc: bool = False, patch_depot_key: bool = False) -> bool:
        """处理不求人库清单下载

        对 Lua 类解锁器（SteamTools / OpenSteam），除了把 .manifest 落进 depotcache，
        还要生成 lua 解锁文件（addappid + addappid(depot,1,key) + setManifestid），
        否则 OpenSteam 只会看到一堆清单文件而不会真正解锁。
        """
        try:
            self.log.info(f'正从 清单不求人库 处理 AppID {app_id} 的清单...')
            
            # 使用steamui API获取depot和manifest信息（复用现有逻辑）
            depot_manifest_map = await self._get_depots_and_manifests_from_steamui(app_id)
            if not depot_manifest_map:
                self.log.error(f"未能获取到 AppID {app_id} 的 depot 信息（已尝试 steamui / ddxnb / steamcmd 三个数据源），请检查APP ID是否正确。")
                return False
            
            self.log.info(f"已获取 AppID {app_id} 的 {len(depot_manifest_map)} 个 depot 及其 manifest")
            
            # 下载所有depot的清单
            success_count = 0
            total_count = len(depot_manifest_map)
            
            for i, (depot_id, manifest_id) in enumerate(depot_manifest_map.items(), 1):
                self.log.info(f"处理进度: {i}/{total_count}")
                depot_name = f"Depot {depot_id}"
                
                # 使用不求人接口下载
                if await self._download_manifest_buqiuren(depot_id, manifest_id, depot_name):
                    success_count += 1
                else:
                    self.log.warning(f"下载 depot {depot_id} 的清单失败")
                
                # 添加延迟避免频率限制
                if i < total_count:
                    delay = random.uniform(2, 4)
                    self.log.info(f"等待 {delay:.1f} 秒后继续...")
                    await asyncio.sleep(delay)
            
            if success_count == 0:
                self.log.error(f"AppID {app_id} 没有成功下载任何清单")
                return False
            
            self.log.info(f"成功处理不求人库清单: 成功 {success_count}/{total_count}")

            # 为 Lua 类解锁器生成 lua 解锁文件
            effective_unlocker = unlocker_type or self.unlocker_type or ""
            if self._is_lua_unlocker(effective_unlocker):
                await self._generate_lua_from_buqiuren(
                    app_id, depot_manifest_map, effective_unlocker,
                    use_st_auto_update, add_all_dlc, patch_depot_key
                )
            return True
            
        except Exception as e:
            self.log.error(f'处理不求人库清单时出错: {self.stack_error(e)}')
            return False

    async def _generate_lua_from_buqiuren(self, app_id: str, depot_manifest_map: Dict[str, str], unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool) -> None:
        """根据不求人库的 depot->manifest 映射生成 lua 解锁文件（尽量补上 depotkey）。"""
        try:
            # 尝试补全 depotkey（拿不到也不影响 setManifestid 生效）
            depotkeys_data = {}
            try:
                depotkeys_data = await self._get_cached_sudama_data() or {}
            except Exception as e:
                self.log.warning(f"获取 depotkey 失败（将继续生成不含密钥的 lua）: {e}")

            lua_dir = self._lua_write_dir(unlocker_type)
            lua_dir.mkdir(parents=True, exist_ok=True)
            lua_filepath = lua_dir / f"{app_id}.lua"

            tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
            self.log.info(f"{tool_label} 自动更新模式: {'已启用' if use_st_auto_update else '已禁用'}")

            matched_keys = 0
            async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as lua_file:
                await lua_file.write(f'addappid({app_id})\n')

                for depot_id in depot_manifest_map.keys():
                    key = depotkeys_data.get(str(depot_id))
                    if key and str(key).strip():
                        await lua_file.write(f'addappid({depot_id}, 1, "{str(key).strip()}")\n')
                        matched_keys += 1

                for depot_id, manifest_id in depot_manifest_map.items():
                    line = f'setManifestid({depot_id}, "{manifest_id}")\n'
                    await lua_file.write('--' + line if use_st_auto_update else line)

            self.log.info(f"已为{tool_label}生成解锁文件: {lua_filepath}（匹配到 {matched_keys}/{len(depot_manifest_map)} 个 depotkey）")

            if add_all_dlc:
                await self._add_free_dlcs_to_lua(app_id, lua_filepath)

            if patch_depot_key:
                self.log.info("开始修补创意工坊depotkey...")
                await self.patch_lua_with_depotkey(app_id, lua_filepath)
        except Exception as e:
            self.log.error(f"为不求人库生成 lua 时出错: {self.stack_error(e)}")

    # NEW: DepotKey patching methods
    async def download_depotkeys_json(self) -> Dict | None:
        """
        获取 DepotKeys 数据。
        已修改：不再从 GitHub/ManifestHub 下载，而是直接复用 Sudama API 的缓存逻辑。
        这样 '修补创意工坊密钥' 功能也会使用 Sudama 的数据源。
        """
        self.log.info("正在获取 DepotKeys (来源: Sudama API)...")
        return await self._get_cached_sudama_data()

    async def process_steamautocracks_v2_manifest(self, app_id: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool = False, patch_depot_key: bool = False) -> bool:
        """处理 SteamAutoCracks/ManifestHub(2) 清单库 - 使用 steamui API 获取 depot 和 manifest 信息"""
        try:
            self.log.info(f'正从 SteamAutoCracks/ManifestHub(2) 处理 AppID {app_id} 的清单...')
            
            # 1. 从 steamui API 获取 depot 和 manifest 信息
            depot_manifest_map = await self._get_depots_and_manifests_from_steamui(app_id)
            if not depot_manifest_map:
                self.log.error(f"未能获取到 AppID {app_id} 的 depot 信息（已尝试 steamui / ddxnb / steamcmd 三个数据源），请检查APP ID是否正确。")
                return False
            
            self.log.info(f"已获取 AppID {app_id} 的 {len(depot_manifest_map)} 个 depot 及其 manifest")
            
            # 2. 下载 depotkeys.json（复用现有方法）
            if 'IS_CN' not in os.environ:
                self.log.info("检测网络环境以优化下载源选择...")
                await self.checkcn()
            
            depotkeys_data = await self.download_depotkeys_json()
            if not depotkeys_data:
                self.log.error("无法获取 depotkeys 数据")
                return False
            
            # 3. 匹配 depot 与 depotkey
            valid_depots = {}
            for depot_id in depot_manifest_map.keys():
                if depot_id in depotkeys_data:
                    depotkey = depotkeys_data[depot_id]
                    # 检查 depotkey 是否有效（不为空字符串）
                    if depotkey and str(depotkey).strip():
                        valid_depots[depot_id] = str(depotkey).strip()
                        self.log.info(f"找到 depot {depot_id} 的有效 depotkey: {depotkey}")
                    else:
                        self.log.warning(f"depot {depot_id} 的 depotkey 为空，自动跳过")
                else:
                    self.log.warning(f"未找到 depot {depot_id} 的 depotkey，自动跳过")
            
            if not valid_depots:
                self.log.warning(f"AppID {app_id} 没有找到任何有效的 depot 密钥，这是正常情况，可能此APP ID没有创意工坊密钥或者暂未收录，不影响本体使用")
                return False
            
            # 4. 根据解锁工具类型处理
            if self._is_lua_unlocker(unlocker_type):
                return await self._process_steamautocracks_v2_for_steamtools(app_id, valid_depots, depot_manifest_map, use_st_auto_update, add_all_dlc, patch_depot_key, depotkeys_data, unlocker_type)
            else:
                return await self._process_steamautocracks_v2_for_greenluma(app_id, valid_depots)
                
        except Exception as e:
            self.log.error(f'处理 SteamAutoCracks/ManifestHub(2) 清单时出错: {self.stack_error(e)}')
            return False

    async def _get_depots_and_manifests_from_ddxnb(self, app_id: str) -> Dict[str, str]:
        """从备用API (steam.ddxnb.cn) 获取 depot 和对应的 manifest 信息"""
        try:
            url = f"https://steam.ddxnb.cn/v1/info/{app_id}"
            response = await self.client.get(url, timeout=20)
            response.raise_for_status()

            data = response.json()
            
            # Check for success status in the API response
            if data.get("status") != "success" or not data.get("data"):
                self.log.error(f"备用API返回错误或无数据 for AppID {app_id}，请检查APP ID是否正确或API请求问题")
                return {}

            app_data = data["data"].get(app_id)
            if not app_data or "depots" not in app_data:
                self.log.error(f"备用API响应中未找到 AppID {app_id} 的 depots 信息，请检查APP ID是否正确或API请求问题")
                return {}
            
            depots = app_data["depots"]
            depot_manifest_map = {}

            for depot_id, depot_info in depots.items():
                if not depot_id.isdigit():
                    continue

                if isinstance(depot_info, dict):
                    manifests = depot_info.get("manifests", {})
                    public_manifest = manifests.get("public", {})
                    manifest_id = public_manifest.get("gid")

                    if manifest_id:
                        depot_manifest_map[depot_id] = str(manifest_id)
                        self.log.info(f"从备用API发现有效 depot: {depot_id}, manifest: {manifest_id}")

            if depot_manifest_map:
                self.log.info(f"从备用API总共找到 {len(depot_manifest_map)} 个有效的 depot 及其 manifest")
            else:
                self.log.warning(f"备用API未找到 AppID {app_id} 的任何有效 depot-manifest 映射，请检查APP ID是否正确或API请求问题")

            return depot_manifest_map

        except Exception as e:
            self.log.error(f"从备用API (steam.ddxnb.cn) 获取 depot 信息失败: {e}")
            return {}

    async def _get_depots_and_manifests_from_steamui(self, app_id: str) -> Dict[str, str]:
        """从 steamui API 获取 depot 和对应的 manifest 信息，失败时使用备用API"""
        # 1. 尝试主API (steamui.com)
        vdf_content = "" # Initialize to ensure it exists for logging on failure
        try:
            self.log.info(f"正从主API (steamui.com) 获取 AppID {app_id} 的信息...")
            url = f"https://steamui.com/api/get_appinfo.php?appid={app_id}"
            response = await self.client.get(url, timeout=20)
            response.raise_for_status()
            
            vdf_content = response.text
            
            import vdf # Local import to avoid dependency issues if VDF is not always used
            data = vdf.loads(vdf_content)
            
            depot_manifest_map = {}
            
            # First-level check for depots
            for key, value in data.items():
                if key.isdigit() and isinstance(value, dict):
                    if 'manifests' in value and value['manifests']:
                        manifests = value['manifests']
                        if isinstance(manifests, dict) and 'public' in manifests:
                            public_manifest = manifests['public']
                            if isinstance(public_manifest, dict) and 'gid' in public_manifest:
                                manifest_id = public_manifest['gid']
                                depot_manifest_map[key] = manifest_id
            
            # Fallback checks for different VDF structures if first-level fails
            if not depot_manifest_map:
                if 'depots' in data:
                    depots = data['depots']
                    for depot_id, depot_info in depots.items():
                        if depot_id.isdigit() and isinstance(depot_info, dict):
                            if 'manifests' in depot_info and depot_info['manifests']:
                                manifests = depot_info['manifests']
                                if isinstance(manifests, dict) and 'public' in manifests:
                                    public_manifest = manifests['public']
                                    if isinstance(public_manifest, dict) and 'gid' in public_manifest:
                                        manifest_id = public_manifest['gid']
                                        depot_manifest_map[depot_id] = manifest_id
                
                if not depot_manifest_map:
                    for key, value in data.items():
                        if isinstance(value, dict) and 'depots' in value:
                            depots = value['depots']
                            for depot_id, depot_info in depots.items():
                                if depot_id.isdigit() and isinstance(depot_info, dict):
                                    if 'manifests' in depot_info and depot_info['manifests']:
                                        manifests = depot_info['manifests']
                                        if isinstance(manifests, dict) and 'public' in manifests:
                                            public_manifest = manifests['public']
                                            if isinstance(public_manifest, dict) and 'gid' in public_manifest:
                                                manifest_id = public_manifest['gid']
                                                depot_manifest_map[depot_id] = manifest_id

            if depot_manifest_map:
                self.log.info(f"从主API (steamui.com) 成功获取 {len(depot_manifest_map)} 个 depot。")
                return depot_manifest_map
            else:
                # This case means the request was successful but no depots were found.
                # We should raise an exception to trigger the fallback.
                raise ValueError("主API响应成功，但未解析到任何depot信息，请检查APP ID是否正确或API请求问题")

        except Exception as e:
            self.log.warning(f"主API (steamui.com) 访问或解析失败: {e}。")
            if vdf_content:
                self.log.warning(f"主API返回内容预览: {vdf_content[:300]}...")
            self.log.info("正在尝试备用API (steam.ddxnb.cn)...")

        # 2. 如果主API失败，调用备用API
        depot_manifest_map = await self._get_depots_and_manifests_from_ddxnb(app_id)
        if depot_manifest_map:
            return depot_manifest_map

        # 3. 备用API 也失败时，降级到 SteamCMD API（结构与 ddxnb 一致）
        self.log.info("正在尝试 SteamCMD API (api.steamcmd.net)...")
        return await self._get_depots_and_manifests_from_steamcmd(app_id)

    async def _get_depots_and_manifests_from_steamcmd(self, app_id: str) -> Dict[str, str]:
        """从 SteamCMD API (api.steamcmd.net) 获取 depot 和对应的 manifest 信息。
        该接口返回 {"status":"success","data":{"<appid>":{"depots":{...}}}}，结构与 ddxnb 一致。"""
        try:
            url = f"https://api.steamcmd.net/v1/info/{app_id}"
            data = await self.http_get_safe(url, timeout=25, max_retries=3, retry_delay=2.0)
            if data is None:
                self.log.warning("SteamCMD API 无响应，跳过。")
                return {}

            payload = data.json()
            if payload.get("status") != "success":
                self.log.warning(f"SteamCMD API 返回状态异常: {payload.get('status')}")
                return {}

            app_data = (payload.get("data") or {}).get(str(app_id)) or {}
            depots = app_data.get("depots") or {}
            depot_manifest_map = {}
            for depot_id, depot_info in depots.items():
                if not str(depot_id).isdigit() or not isinstance(depot_info, dict):
                    continue
                public_manifest = (depot_info.get("manifests") or {}).get("public") or {}
                manifest_id = public_manifest.get("gid")
                if manifest_id:
                    depot_manifest_map[str(depot_id)] = str(manifest_id)

            if depot_manifest_map:
                self.log.info(f"从 SteamCMD API 成功获取 {len(depot_manifest_map)} 个 depot。")
            else:
                self.log.warning(f"SteamCMD API 未找到 AppID {app_id} 的任何有效 depot-manifest 映射。")
            return depot_manifest_map

        except Exception as e:
            self.log.error(f"从 SteamCMD API 获取 depot 信息失败: {e}")
            return {}

    async def _process_steamautocracks_v2_for_steamtools(self, app_id: str, valid_depots: Dict[str, str], depot_manifest_map: Dict[str, str], use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool, depotkeys_data: Dict, unlocker_type: str = "steamtools") -> bool:
        """为 SteamTools / OpenSteam 处理 SteamAutoCracks/ManifestHub(2) 清单"""
        try:
            stplug_path = self._lua_write_dir(unlocker_type)
            stplug_path.mkdir(parents=True, exist_ok=True)
            
            lua_filename = f"{app_id}.lua"
            lua_filepath = stplug_path / lua_filename
            
            # 检查是否启用了自动更新模式
            is_auto_update_mode = use_st_auto_update
            
            # 生成 lua 文件内容
            lines = []
            
            # 第一行：主游戏 appid
            lines.append(f'addappid({app_id})')
            
            # 添加所有有效的 depot 及其密钥
            for depot_id, depotkey in valid_depots.items():
                lines.append(f'addappid({depot_id}, 1, "{depotkey}")')
            
            # 添加 setManifestid 行（使用从 steamui API 获取的 manifest 信息）
            manifest_lines = []
            for depot_id in valid_depots.keys():
                if depot_id in depot_manifest_map:
                    manifest_id = depot_manifest_map[depot_id]
                    # 根据是否启用自动更新决定是否注释掉 manifest 行
                    if is_auto_update_mode:
                        # 自动更新模式：注释掉 setManifestid 行
                        manifest_lines.append(f'--setManifestid({depot_id}, "{manifest_id}")')
                        self.log.info(f"添加注释的 manifest 映射（自动更新模式）: depot {depot_id} -> manifest {manifest_id}")
                    else:
                        # 固定版本模式：正常添加 setManifestid 行
                        manifest_lines.append(f'setManifestid({depot_id}, "{manifest_id}")')
                        self.log.info(f"添加 manifest 映射（固定版本）: depot {depot_id} -> manifest {manifest_id}")
            
            # 写入文件
            async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as lua_file:
                await lua_file.write('\n'.join(lines) + '\n')
                if manifest_lines:
                    await lua_file.write('\n-- Manifests\n')
                    await lua_file.write('\n'.join(manifest_lines) + '\n')
            
            tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
            self.log.info(f"已为{tool_label}生成解锁文件: {lua_filepath}")
            
            # 处理 DLC
            if add_all_dlc:
                await self._add_free_dlcs_to_lua(app_id, lua_filepath)
            
            # 处理创意工坊密钥修补（复用已下载的 depotkeys_data）
            if patch_depot_key:
                self.log.info("开始修补创意工坊depotkey...")
                await self._patch_lua_with_existing_depotkeys(app_id, lua_filepath, depotkeys_data)
            
            return True
            
        except Exception as e:
            self.log.error(f'为 SteamTools 处理 SteamAutoCracks/ManifestHub(2) 清单时出错: {e}')
            return False

    async def _process_steamautocracks_v2_for_greenluma(self, app_id: str, valid_depots: Dict[str, str]) -> bool:
        """为 GreenLuma 处理 SteamAutoCracks/ManifestHub(2) 清单"""
        try:
            # GreenLuma needs the depotkeys merged into config.vdf
            depots_config = {'depots': {depot_id: {"DecryptionKey": key} for depot_id, key in valid_depots.items()}}
            
            # Merge depotkeys
            config_vdf_path = self.steam_path / 'config' / 'config.vdf'
            if await self.depotkey_merge(config_vdf_path, depots_config):
                self.log.info("已将密钥合并到 config.vdf")
            
            # Add app IDs to GreenLuma
            gl_ids = list(valid_depots.keys())
            gl_ids.append(app_id)
            await self.greenluma_add(list(set(gl_ids)))
            self.log.info("已添加到 GreenLuma")
            
            return True
            
        except Exception as e:
            self.log.error(f'为 GreenLuma 处理 SteamAutoCracks/ManifestHub(2) 清单时出错: {e}')
            return False
    
    async def _patch_lua_with_existing_depotkeys(self, app_id: str, lua_file_path: Path, depotkeys_data: Dict) -> bool:
        """使用已有的 depotkeys 数据修补 LUA 文件（避免重复下载）"""
        try:
            # 检查 app_id 是否在 depotkeys 中
            if app_id not in depotkeys_data:
                self.log.warning(f"没有此AppID的depotkey: {app_id}，这是正常情况，可能此APP ID没有创意功放密钥或者暂未收录，不影响本体使用")
                return False
            
            depotkey = depotkeys_data[app_id]
            
            # 检查 depotkey 是否有效
            if not depotkey or not str(depotkey).strip():
                self.log.warning(f"AppID {app_id} 的 depotkey 为空或无效，跳过修补: '{depotkey}，，这是正常情况，可能此APP ID没有创意功放密钥或者暂未收录，不影响本体使用'")
                return False
            
            depotkey = str(depotkey).strip()
            self.log.info(f"找到 AppID {app_id} 的有效 depotkey: {depotkey}")
            
            # 读取现有 LUA 文件
            if not lua_file_path.exists():
                self.log.error(f"LUA文件不存在: {lua_file_path}")
                return False
            
            async with aiofiles.open(lua_file_path, 'r', encoding='utf-8') as f:
                lua_content = await f.read()
            
            # 解析行
            lines = lua_content.strip().split('\n')
            new_lines = []
            app_id_line_removed = False
            
            # 移除现有的 addappid({app_id}) 行并添加带 depotkey 的新行
            for line in lines:
                line = line.strip()
                # 检查是否是需要替换的简单 addappid 行
                if line == f"addappid({app_id})":
                    # 替换为带 depotkey 的版本
                    new_lines.append(f'addappid({app_id},1,"{depotkey}")')
                    app_id_line_removed = True
                    self.log.info(f"已替换: addappid({app_id}) -> addappid({app_id},1,\"{depotkey}\")")
                else:
                    new_lines.append(line)
            
            # 如果没有找到简单的 addappid 行，添加 depotkey 版本
            if not app_id_line_removed:
                new_lines.append(f'addappid({app_id},1,"{depotkey}")')
                self.log.info(f"已添加新的 depotkey 条目: addappid({app_id},1,\"{depotkey}\")")
            
            # 写回文件
            async with aiofiles.open(lua_file_path, 'w', encoding='utf-8') as f:
                await f.write('\n'.join(new_lines) + '\n')
            
            self.log.info(f"成功修补 LUA 文件的 depotkey: {lua_file_path.name}")
            return True
            
        except Exception as e:
            self.log.error(f"修补 LUA depotkey 时出错: {self.stack_error(e)}")
            return False

    async def patch_lua_with_depotkey(self, app_id: str, lua_file_path: Path) -> bool:
        """Patch LUA file with depotkey from SteamAutoCracks repository"""
        try:
            # Ensure network environment is detected for mirror selection
            if 'IS_CN' not in os.environ:
                self.log.info("检测网络环境以优化下载源选择...")
                await self.checkcn()
            
            # Download depotkeys.json
            depotkeys_data = await self.download_depotkeys_json()
            if not depotkeys_data:
                self.log.error("无法获取 depotkeys 数据，跳过 depotkey 修补。")
                return False
            
            # Check if app_id exists in depotkeys
            if app_id not in depotkeys_data:
                self.log.warning(f"没有此AppID的depotkey: {app_id}，，这是正常情况，可能此APP ID没有创意功放密钥或者暂未收录，不影响本体使用")
                return False
            
            depotkey = depotkeys_data[app_id]
            
            # FIXED: Check if depotkey is valid (not empty, not None, not just whitespace)
            if not depotkey or not str(depotkey).strip():
                self.log.warning(f"AppID {app_id} 的 depotkey 为空或无效，跳过修补: '{depotkey}，这是正常情况，可能此APP ID没有创意功放密钥或者暂未收录，不影响本体使用'")
                return False
            
            # Make sure depotkey is string and strip whitespace
            depotkey = str(depotkey).strip()
            self.log.info(f"找到 AppID {app_id} 的有效 depotkey: {depotkey}")
            
            # Read existing LUA file
            if not lua_file_path.exists():
                self.log.error(f"LUA文件不存在: {lua_file_path}")
                return False
            
            async with aiofiles.open(lua_file_path, 'r', encoding='utf-8') as f:
                lua_content = await f.read()
            
            # Parse lines
            lines = lua_content.strip().split('\n')
            new_lines = []
            app_id_line_removed = False
            
            # Remove existing addappid({app_id}) line and add new one with depotkey
            for line in lines:
                line = line.strip()
                # Check if this is the simple addappid line we need to replace
                if line == f"addappid({app_id})":
                    # Replace with depotkey version
                    new_lines.append(f'addappid({app_id},1,"{depotkey}")')
                    app_id_line_removed = True
                    self.log.info(f"已替换: addappid({app_id}) -> addappid({app_id},1,\"{depotkey}\")")
                else:
                    new_lines.append(line)
            
            # If we didn't find the simple addappid line, add the depotkey version
            if not app_id_line_removed:
                new_lines.append(f'addappid({app_id},1,"{depotkey}")')
                self.log.info(f"已添加新的 depotkey 条目: addappid({app_id},1,\"{depotkey}\")")
            
            # Write back to file
            async with aiofiles.open(lua_file_path, 'w', encoding='utf-8') as f:
                await f.write('\n'.join(new_lines) + '\n')
            
            self.log.info(f"成功修补 LUA 文件的 depotkey: {lua_file_path.name}")
            return True
            
        except Exception as e:
            self.log.error(f"修补 LUA depotkey 时出错: {self.stack_error(e)}")
            return False

    # Original methods continue...
    def restart_steam(self) -> bool:
        if not self.steam_path:
            self.log.error("无法重启 Steam：未找到 Steam 路径。")
            return False
        steam_exe_path = self.steam_path / 'steam.exe'
        if not steam_exe_path.exists():
            self.log.error(f"无法启动 Steam：在 '{self.steam_path}' 目录下未找到 steam.exe。")
            return False
        try:
            self.log.info("正在尝试关闭正在运行的 Steam 进程...")
            result = subprocess.run(["taskkill", "/F", "/IM", "steam.exe"], capture_output=True, text=True, check=False)
            if result.returncode == 0: self.log.info("成功关闭 Steam 进程。")
            elif result.returncode == 128: self.log.info("未找到正在运行的 Steam 进程，将直接启动。")
            else: self.log.warning(f"关闭 Steam 时遇到问题 (返回码: {result.returncode})。错误信息: {result.stderr.strip()}")

            # 若当前使用 OpenSteam(OpenSteamKitten)，先关闭旧实例，重启 Steam 后再拉起
            opensteam_exe = self._get_opensteam_exe_path()
            if opensteam_exe is not None:
                self.log.info("检测到 OpenSteamKitten，正在关闭旧实例...")
                subprocess.run(["taskkill", "/F", "/IM", OPENSTEAM_EXE_NAME], capture_output=True, text=True, check=False)

            self.log.info("等待 3 秒以确保 Steam 完全关闭...")
            time.sleep(3)
            self.log.info(f"正在尝试从 '{steam_exe_path}' 启动 Steam...")
            subprocess.Popen([str(steam_exe_path)], creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
            self.log.info("已发送重启 Steam 的指令。")

            if opensteam_exe is not None:
                # Steam 启动后再拉起 OpenSteamKitten，避免与关闭中的进程争用
                time.sleep(2)
                self.log.info(f"正在启动 OpenSteamKitten: '{opensteam_exe}'...")
                try:
                    subprocess.Popen([str(opensteam_exe)], creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
                    self.log.info("已发送启动 OpenSteamKitten 的指令。")
                except Exception as e:
                    self.log.warning(f"启动 OpenSteamKitten 失败: {e}")
            return True
        except Exception as e:
            self.log.error(f"重启 Steam 失败: {self.stack_error(e)}")
            return False

    async def check_github_api_rate_limit(self) -> bool:
        github_token = self.config.get("Github_Personal_Token", "").strip()
        headers = {'Authorization': f'Bearer {github_token}'} if github_token else None
        if github_token: self.log.info("已配置GitHub Token。")
        else: self.log.warning("未找到GitHub Token。您的API请求将受到严格的速率限制。")
        url = 'https://api.github.com/rate_limit'
        try:
            r = await self.client.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            rate_limit = r.json().get('resources', {}).get('core', {})
            remaining = rate_limit.get('remaining', 0)
            reset_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rate_limit.get('reset', 0)))
            self.log.info(f'GitHub API剩余请求次数: {remaining}')
            if remaining == 0:
                self.log.error("GitHub API请求次数已用尽。")
                self.log.error(f"您的请求次数将于 {reset_time} 重置。")
                self.log.error("要提升请求上限，请在config.json文件中添加您的'Github_Personal_Token'。")
                return False
            return True
        except Exception as e:
            # 只是探测配额失败（网络不可达/被墙），不代表配额用尽；
            # 直接放行，让后续实际请求自己报错，避免整条任务链被卡住。
            self.log.warning(f"无法查询 GitHub API 速率限制（网络不可达？），跳过配额检查继续: {type(e).__name__}")
            return True

    async def checkcn(self) -> bool:
        """探测网络区域并设置 IS_CN 环境变量；结果在当前进程内只打印一次。"""
        if 'IS_CN' in os.environ:
            return os.environ['IS_CN'] == 'yes'
        try:
            req = await self.client.get('https://mips.kugou.com/check/iscn?&format=json', timeout=5)
            body = req.json()
            is_cn = bool(body['flag'])
            os.environ['IS_CN'] = 'yes' if is_cn else 'no'
            self.log.info(f"网络区域: {body.get('country', '未知')}，使用{'国内镜像' if is_cn else 'GitHub 直连'}")
            return is_cn
        except Exception:
            os.environ['IS_CN'] = 'yes'
            self.log.debug('区域探测失败，默认使用国内镜像。')
            return True

    def parse_lua_file_for_depots(self, lua_file_path: str) -> Dict:
        addappid_pattern = re.compile(r'addappid\((\d+),\s*1,\s*"([^"]+)"\)')
        depots = {}
        try:
            with open(lua_file_path, 'r', encoding='utf-8') as file:
                lua_content = file.read()
                for match in addappid_pattern.finditer(lua_content):
                    depots[match.group(1)] = {"DecryptionKey": match.group(2)}
        except Exception as e:
            self.log.error(f"解析lua文件 {lua_file_path} 出错: {e}")
        return depots

    async def depotkey_merge(self, config_path: Path, depots_config: dict) -> bool:
        if not config_path.exists():
            self.log.error('未找到Steam默认配置文件，您可能尚未登录。')
            return False
        try:
            async with aiofiles.open(config_path, encoding='utf-8') as f: content = await f.read()
            config_vdf = vdf.loads(content)
            steam = config_vdf.get('InstallConfigStore', {}).get('Software', {}).get('Valve') or \
                    config_vdf.get('InstallConfigStore', {}).get('Software', {}).get('valve')
            if steam is None:
                self.log.error('找不到Steam配置节。')
                return False
            depots = steam.setdefault('depots', {})
            depots.update(depots_config.get('depots', {}))
            async with aiofiles.open(config_path, mode='w', encoding='utf-8') as f:
                await f.write(vdf.dumps(config_vdf, pretty=True))
            self.log.info('成功将密钥合并到config.vdf。')
            return True
        except Exception as e:
            self.log.error(f'合并失败: {self.stack_error(e)}')
            return False

    async def _get_from_mirrors(self, sha: str, path: str, repo: str) -> bytes:
        urls = [f'https://raw.githubusercontent.com/{repo}/{sha}/{path}']
        if os.environ.get('IS_CN') == 'yes':
            urls = [f'https://gh-proxy.org/https://github.com/{repo}/{sha}/{path}',f'https://cdn.gh-proxy.org/https://github.com/{repo}/{sha}/{path}',f'https://edgeone.gh-proxy.org/https://github.com/{repo}/{sha}/{path}',f'https://github.chenc.dev/github.com/{repo}/{sha}/{path}',f'https://fastgit.cc/https://github.com/{repo}/{sha}/{path}',f'https://gh.llkk.cc/https://github.com/{repo}/{sha}/{path}',f'https://gh.akass.cn/{repo}/{sha}/{path}',f'https://raw.githubusercontent.com/{repo}/{sha}/{path}']
        # 逐个镜像尝试，失败原因只在调试模式下逐条输出，成功后不再重复刷屏
        for url in urls:
            try:
                r = await self.client.get(url, timeout=30)
                if r.status_code == 200:
                    self.log.debug(f'下载成功: {path} (来自 {url.split("/")[2]})')
                    return r.content
                self.log.debug(f'镜像 {url.split("/")[2]} 返回 {r.status_code}: {path}')
            except httpx.RequestError as e:
                self.log.debug(f'镜像 {url.split("/")[2]} 请求失败: {path} - {e}')
        raise Exception(f'所有镜像均无法下载文件: {path}')

    async def greenluma_add(self, depot_id_list: list) -> bool:
        app_list_path = self.steam_path / 'AppList'
        try:
            for file in app_list_path.glob('*.txt'): file.unlink(missing_ok=True)
            depot_dict = { int(i.stem): int(i.read_text(encoding='utf-8').strip()) for i in app_list_path.iterdir() if i.is_file() and i.stem.isdecimal() and i.suffix == '.txt' }
            for depot_id in map(int, depot_id_list):
                if depot_id not in depot_dict.values():
                    index = max(depot_dict.keys(), default=-1) + 1
                    (app_list_path / f'{index}.txt').write_text(str(depot_id), encoding='utf-8')
                    depot_dict[index] = depot_id
            self.log.info(f"成功将 {len(depot_id_list)} 个ID添加到GreenLuma的AppList中。")
            return True
        except Exception as e:
            self.log.error(f'GreenLuma添加 AppID失败: {e}')
            return False
            
    async def _get_steamcmd_api_data(self, appid: str) -> Dict:
        try:
            resp = await self.client.get(f"https://api.steamcmd.net/v1/info/{appid}", timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self.log.error(f"从 api.steamcmd.net 获取 AppID {appid} 数据失败: {e}")
            return {}

    # UPDATED: Use new safe functions for DLC retrieval
    async def _get_dlc_ids(self, appid: str) -> List[str]:
        """获取DLC ID列表，使用新的安全函数"""
        return await self.get_dlc_ids_safe(appid)

    # UPDATED: Use new safe functions for depot retrieval  
    async def _get_depots(self, appid: str) -> List[Dict]:
        """获取Depot信息列表，转换为旧格式兼容"""
        depot_tuples = await self.get_depots_safe(appid)
        # Convert to old format for compatibility
        return [
            {
                "depot_id": depot_id,
                "size": size,
                "dlc_appid": source.split(':')[1] if source.startswith('DLC:') else None
            }
            for depot_id, manifest_id, size, source in depot_tuples
        ]

    async def _add_free_dlcs_to_lua(self, app_id: str, lua_filepath: Path):
        self.log.info(f"开始为 AppID {app_id} 查找无密钥/无Depot的DLC...")
        try:
            all_dlc_ids = await self._get_dlc_ids(app_id)
            if not all_dlc_ids:
                self.log.info(f"AppID {app_id} 未找到任何DLC。")
                return

            tasks = [self._get_depots(dlc_id) for dlc_id in all_dlc_ids]
            results = await asyncio.gather(*tasks)

            depot_less_dlc_ids = [dlc_id for dlc_id, dlc_depots in zip(all_dlc_ids, results) if not dlc_depots]
            
            if not depot_less_dlc_ids:
                self.log.info(f"未找到适用于 AppID {app_id} 的无密钥/无Depot的DLC。")
                return

            async with self.lock:
                if not lua_filepath.exists():
                    self.log.error(f"目标LUA文件 {lua_filepath} 不存在，无法合并DLC。")
                    return

                async with aiofiles.open(lua_filepath, 'r', encoding='utf-8') as f:
                    existing_lines = [line.strip() for line in await f.readlines() if line.strip()]
                
                existing_appids = {match.group(1) for line in existing_lines if (match := re.search(r'addappid\((\d+)', line))}
                new_dlcs_to_add = [dlc_id for dlc_id in depot_less_dlc_ids if dlc_id not in existing_appids]
                
                if not new_dlcs_to_add:
                    self.log.info(f"所有找到的无Depot DLC均已存在于解锁文件中。无需添加。")
                    return

                self.log.info(f"找到 {len(new_dlcs_to_add)} 个新的无密钥/无Depot DLC，正在合并到 LUA 文件...")

                final_lines = set(existing_lines)
                for dlc_id in new_dlcs_to_add: final_lines.add(f"addappid({dlc_id})")

                def sort_key(line):
                    match_add = re.search(r'addappid\((\d+)', line)
                    if match_add: return (0, int(match_add.group(1)))
                    match_set = re.search(r'setManifestid\((\d+)', line)
                    if match_set: return (1, int(match_set.group(1)))
                    return (2, line)
                
                sorted_lines = sorted(list(final_lines), key=sort_key)

                async with aiofiles.open(lua_filepath, 'w', encoding='utf-8') as f:
                    await f.write('\n'.join(sorted_lines) + '\n')
            
            self.log.info(f"成功将 {len(new_dlcs_to_add)} 个新的无密钥/无Depot DLC合并到 {lua_filepath.name}")

        except Exception as e:
            self.log.error(f"添加无密钥DLC时出错: {self.stack_error(e)}")

    # MODIFIED: Added patch_depot_key parameter
    async def _process_zip_manifest_generic(self, app_id: str, download_url: str, source_name: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool = False) -> bool:
        zip_path = self.temp_path / f'{app_id}.zip'
        extract_path = self.temp_path / app_id
        try:
            self.temp_path.mkdir(exist_ok=True, parents=True)
            self.log.info(f'正从 {source_name} 下载 AppID {app_id} 的清单...')
            response = await self.client.get(download_url, timeout=60)
            response.raise_for_status()
            async with aiofiles.open(zip_path, 'wb') as f: await f.write(response.content)
            # 校验下载内容真的是 zip：部分镜像会返回 "OK"/HTML 错误页，直接用 zipfile 打开只会抛 BadZipFile
            if not zipfile.is_zipfile(zip_path):
                preview = response.content[:120]
                try:
                    preview_text = preview.decode('utf-8', errors='ignore').strip()
                except Exception:
                    preview_text = repr(preview)
                self.log.error(f'{source_name} 返回的内容不是有效的 zip（{len(response.content)} 字节，开头: {preview_text[:100]!r}）。'
                               f'该源可能已失效或不提供此 AppID，请换用其他清单源。')
                return False
            self.log.info('正在解压...')
            with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(extract_path)
            
            st_files = list(extract_path.glob('*.st'))
            if st_files:
                st_converter = STConverter()
                for st_file in st_files:
                    try:
                        lua_content = st_converter.convert_file(str(st_file))
                        (st_file.with_suffix('.lua')).write_text(lua_content, encoding='utf-8')
                        self.log.info(f'已转换 {st_file.name} -> {st_file.with_suffix(".lua").name}')
                    except Exception as e: self.log.error(f'转换 .st 文件 {st_file.name} 失败: {e}')

            manifest_files = list(extract_path.glob('*.manifest'))
            lua_files = list(extract_path.glob('*.lua'))
            
            if self._is_lua_unlocker(unlocker_type):
                tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
                self.log.info(f"{tool_label} 自动更新模式: {'已启用' if use_st_auto_update else '已禁用'}")
                stplug_path = self._lua_write_dir(unlocker_type)
                stplug_path.mkdir(parents=True, exist_ok=True)
                
                all_depots = {}
                for lua_f in lua_files:
                    depots = self.parse_lua_file_for_depots(str(lua_f))
                    all_depots.update(depots)

                lua_filename = f"{app_id}.lua"
                lua_filepath = stplug_path / lua_filename
                async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as lua_file:
                    await lua_file.write(f'addappid({app_id})\n')
                    for depot_id, info in all_depots.items():
                        await lua_file.write(f'addappid({depot_id}, 1, "{info["DecryptionKey"]}")\n')

                    for manifest_f in manifest_files:
                        match = re.search(r'(\d+)_(\w+)\.manifest', manifest_f.name)
                        if match:
                            line = f'setManifestid({match.group(1)}, "{match.group(2)}")\n'
                            if use_st_auto_update: await lua_file.write('--' + line)
                            else: await lua_file.write(line)
                tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
                self.log.info(f"已为 {tool_label} 生成解锁文件: {lua_filepath}")

                if add_all_dlc:
                    await self._add_free_dlcs_to_lua(app_id, lua_filepath)

                # NEW: Apply depotkey patch if requested
                if patch_depot_key:
                    self.log.info("开始修补创意工坊depotkey...")
                    await self.patch_lua_with_depotkey(app_id, lua_filepath)

            else:
                self.log.info(f'检测到 GreenLuma/标准模式，将处理来自 {source_name} 的文件。')
                if not manifest_files:
                    self.log.warning(f"在来自 {source_name} 的压缩包中未找到 .manifest 文件。")
                    return False

                steam_depot_path = self.steam_path / 'depotcache'
                for f in manifest_files:
                    shutil.copy2(f, steam_depot_path / f.name)
                    self.log.info(f'已复制清单: {f.name}')
                
                all_depots = {}
                for lua in lua_files:
                    depots = self.parse_lua_file_for_depots(str(lua))
                    all_depots.update(depots)
                if all_depots:
                    await self.depotkey_merge(self.steam_path / 'config' / 'config.vdf', {'depots': all_depots})

            self.log.info(f'成功处理来自 {source_name} 的清单。')
            return True
        except Exception as e:
            self.log.error(f'处理来自 {source_name} 的清单时出错: {self.stack_error(e)}')
            return False
        finally:
            if zip_path.exists(): zip_path.unlink(missing_ok=True)
            if extract_path.exists(): shutil.rmtree(extract_path)

    async def process_steamtools_games_manifest(self, app_id: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool = False, patch_depot_key: bool = False) -> bool:
        """从 steamtools.games 公开 API 获取清单与 lua。

        该站对外提供无需 key 的 REST 接口（2026-11 实测可用）：
          POST /api/generate  {"appId": "<appid>"}  -> 返回 luaUrl / keyVdfUrl / downloadUrl
          GET  /api/files/{appid}/lua               -> 直接拿到成品 lua
          GET  /api/files/{appid}/key               -> key.vdf（depots.<depot>.DecryptionKey）
          GET  /api/files/{appid}/zip               -> <appid>_public.lua + key.vdf + README.txt
        它的清单库是 6 万+ appid 分支的 ManifestHub3，比 Auiowu 覆盖广得多，
        冷门 / 新作命中率明显更高。
        """
        try:
            self.log.info(f'正从 steamtools.games 处理 AppID {app_id} 的清单...')
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://steamtools.games/",
            }

            # 1. 查询覆盖情况与游戏名
            resp = await self.client.post(
                "https://steamtools.games/api/generate",
                json={"appId": str(app_id)},
                headers=headers,
                timeout=60,
                follow_redirects=True,
            )
            if resp.status_code == 429:
                self.log.warning("steamtools.games 请求过于频繁 (429)，请稍后再试。")
                return False
            if resp.status_code != 200:
                self.log.error(f"steamtools.games 返回 HTTP{resp.status_code}。")
                return False

            payload = resp.json()
            if payload.get("code") != 0:
                self.log.warning(f"steamtools.games: {payload.get('message', '未收录该 AppID')}")
                return False

            info = payload.get("data") or {}
            game_name = info.get("gameName") or ""
            if game_name:
                self.log.info(f"steamtools.games 命中: {game_name}")

            # 2. 直接取成品 lua（站方已按 ManifestHub3 的 depot/manifest 生成好）
            lua_resp = await self.client.get(
                f"https://steamtools.games/api/files/{app_id}/lua",
                headers=headers, timeout=90, follow_redirects=True,
            )
            if lua_resp.status_code != 200 or not lua_resp.text.strip():
                self.log.error(f"steamtools.games 未返回有效 lua（HTTP{lua_resp.status_code}）。")
                return False

            lua_text = lua_resp.text
            self.log.info(f"已获取 lua 内容（{len(lua_text)} 字节）。")

            # 3. 取 key.vdf 补全 depotkey（lua 里已有的不重复加）
            depot_keys = {}
            try:
                key_resp = await self.client.get(
                    f"https://steamtools.games/api/files/{app_id}/key",
                    headers=headers, timeout=60, follow_redirects=True,
                )
                if key_resp.status_code == 200 and key_resp.text.strip():
                    parsed = vdf.loads(key_resp.text)
                    for depot_id, depot_info in (parsed.get("depots") or {}).items():
                        if isinstance(depot_info, dict) and depot_info.get("DecryptionKey"):
                            depot_keys[str(depot_id)] = str(depot_info["DecryptionKey"])
                    self.log.info(f"key.vdf 提供 {len(depot_keys)} 个 depotkey。")
            except Exception as e:
                self.log.warning(f"读取 key.vdf 失败（不影响主要流程）: {e}")

            # 4. 写入
            if self._is_lua_unlocker(unlocker_type):
                lua_dir = self._lua_write_dir(unlocker_type)
                lua_dir.mkdir(parents=True, exist_ok=True)
                lua_filepath = lua_dir / f"{app_id}.lua"

                lines = [lua_text.rstrip("\n")]
                existing = set(re.findall(r'addappid\(\s*(\d+)\s*,', lua_text))
                for depot_id, key in depot_keys.items():
                    if depot_id not in existing and depot_id != str(app_id):
                        lines.append(f'addappid({depot_id}, 1, "{key}")')

                content = "\n".join(lines) + "\n"

                async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as f:
                    await f.write(content)

                tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
                self.log.info(f"已为{tool_label}生成解锁文件: {lua_filepath}")

                if add_all_dlc:
                    await self._add_free_dlcs_to_lua(app_id, lua_filepath)
                if patch_depot_key:
                    self.log.info("开始修补创意工坊depotkey...")
                    await self.patch_lua_with_depotkey(app_id, lua_filepath)
            else:
                # GreenLuma：把 zip 里的 .manifest 落进 depotcache
                zip_resp = await self.client.get(
                    f"https://steamtools.games/api/files/{app_id}/zip",
                    headers=headers, timeout=180, follow_redirects=True,
                )
                if zip_resp.status_code != 200 or not zipfile.is_zipfile(io.BytesIO(zip_resp.content)):
                    self.log.error("steamtools.games 未返回有效 zip，GreenLuma 模式无法继续。")
                    return False

                depot_cache_path = self.steam_path / 'depotcache'
                depot_cache_path.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
                    saved = 0
                    for name in zf.namelist():
                        if name.lower().endswith('.manifest'):
                            (depot_cache_path / Path(name).name).write_bytes(zf.read(name))
                            self.log.info(f"已为 GreenLuma 保存清单: {Path(name).name}")
                            saved += 1
                if not saved:
                    self.log.error("zip 中没有 .manifest 文件。")
                    return False

                if depot_keys:
                    await self.depotkey_merge(
                        self.steam_path / 'config' / 'config.vdf',
                        {'depots': {d: {"DecryptionKey": k} for d, k in depot_keys.items()}},
                    )
                    ids = list(depot_keys.keys()) + [str(app_id)]
                    await self.greenluma_add(list(set(ids)))
                    self.log.info("已合并密钥并添加到GreenLuma。")

            return True

        except Exception as e:
            self.log.error(f"处理 steamtools.games 清单时出错: {self.stack_error(e)}")
            return False

    async def process_zip_source(self, app_id: str, tool_type: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool = False) -> bool:
        source_map = {
            "printedwaste": "https://api.printedwaste.com/gfk/download/{app_id}",
            "cysaw": "https://cysaw.top/uploads/{app_id}.zip",
            "furcate": "https://furcate.eu/files/{app_id}.zip",
            "walftech": "https://walftech.com/proxy.php?url=https%3A%2F%2Fsteamgames554.s3.us-east-1.amazonaws.com%2F{app_id}.zip",
            "steamdatabase": "https://steamdatabase.s3.eu-north-1.amazonaws.com/{app_id}.zip",
            "steamautocracks_v2": "special",  # 特殊处理标识
            "buqiuren": "special",
            "steamtools_games": "special",
            "sudama": "special"
        }
        source_name_map = { 
            "printedwaste": "SWA V2 (printedwaste)", 
            "cysaw": "Cysaw", 
            "furcate": "Furcate", 
            "walftech": "Walftech", 
            "steamdatabase": "SteamDatabase",
            "steamautocracks_v2": "SteamAutoCracks/ManifestHub(2)"
        }
        
        # 特殊处理 steamautocracks_v2
        if tool_type == "steamautocracks_v2":
            return await self.process_steamautocracks_v2_manifest(app_id, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key)
        if tool_type == "buqiuren":
            return await self.process_buqiuren_manifest(app_id, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key)

        if tool_type == "steamtools_games":
            return await self.process_steamtools_games_manifest(app_id, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key)
            
        if tool_type == "sudama":
            return await self.process_sudama_manifest(app_id, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key)
            
        # Check for custom zip repos
        custom_zip_repos = self.get_custom_zip_repos()
        for repo_config in custom_zip_repos:
            if tool_type == f"custom_zip_{repo_config['name']}":
                return await self.process_custom_zip_manifest(app_id, repo_config, add_all_dlc, patch_depot_key)
        
        url_template = source_map.get(tool_type)
        source_name = source_name_map.get(tool_type)
        if not url_template or not source_name:
            self.log.error(f"未知的压缩包源: {tool_type}")
            return False
        download_url = url_template.format(app_id=app_id)
        return await self._process_zip_manifest_generic(app_id, download_url, source_name, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key)

    async def fetch_branch_info(self, url: str, headers: Dict) -> Dict | None:
        try:
            r = await self.client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403: self.log.error("GitHub API请求次数已用尽。")
            elif e.response.status_code != 404: self.log.error(f"从 {url} 获取信息失败: {self.stack_error(e)}")
            return None
        except Exception as e:
            self.log.error(f'从 {url} 获取信息时发生意外错误: {self.stack_error(e)}')
            return None
            
    # MODIFIED: Updated to use all github repos including custom ones
    async def search_all_repos_for_appid(self, app_id: str, repos: List[str] = None) -> List[Dict]:
        """Search for app_id in all GitHub repositories (builtin + custom)"""
        if repos is None:
            repos = self.get_all_github_repos()
        
        github_token = self.config.get("Github_Personal_Token", "")
        headers = {'Authorization': f'Bearer {github_token}'} if github_token else None
        tasks = [self._search_single_repo(app_id, repo, headers) for repo in repos]
        results = await asyncio.gather(*tasks)
        return [res for res in results if res]

    async def _search_single_repo(self, app_id: str, repo: str, headers: Dict) -> Dict | None:
        self.log.info(f"正在仓库 {repo} 中搜索 AppID: {app_id}")
        url = f'https://api.github.com/repos/{repo}/branches/{app_id}'
        r_json = await self.fetch_branch_info(url, headers)
        if r_json and 'commit' in r_json:
            tree_url = r_json['commit']['commit']['tree']['url']
            r2_json = await self.fetch_branch_info(tree_url, headers)
            if r2_json and 'tree' in r2_json:
                self.log.info(f"在 {repo} 中找到清单。")
                return {'repo': repo, 'sha': r_json['commit']['sha'], 'tree': r2_json['tree'], 'update_date': r_json["commit"]["commit"]["author"]["date"]}
        return None

    # MODIFIED: Added patch_depot_key parameter
    async def _process_github_repo_via_codeload(self, app_id: str, repo: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool = False) -> bool:
        """通过 codeload 分支 zip 处理 GitHub 清单仓库（不消耗 GitHub API 配额）。

        `https://codeload.github.com/{repo}/zip/refs/heads/{appid}` 直接把整个分支打成 zip，
        内含 Key.vdf / config.json / *.manifest，与 API + raw 逐文件下载等价，但不计入
        api.github.com 的 60 次/小时匿名配额，也不会因配额耗尽而中断。
        """
        import io as _io
        codeload_url = f"https://codeload.github.com/{repo}/zip/refs/heads/{app_id}"
        try:
            self.log.info(f"正在通过 codeload 拉取 {repo} 分支 {app_id} ...")
            resp = await self.client.get(codeload_url, timeout=180, follow_redirects=True)
            if resp.status_code != 200:
                self.log.error(f"codeload 拉取失败: HTTP{resp.status_code}。如果该清单在 {repo} 中不存在分支 {app_id}，这是正常现象。")
                return False

            if not zipfile.is_zipfile(_io.BytesIO(resp.content)):
                self.log.error(f"codeload 返回的内容不是有效的 zip（{len(resp.content)} 字节），该分支可能不存在。")
                return False

            zf = zipfile.ZipFile(_io.BytesIO(resp.content))
            names = zf.namelist()

            all_manifest_paths_in_tree = [n for n in names if n.lower().endswith('.manifest')]
            key_vdf_path = next((n for n in names if n.lower().endswith('key.vdf')), None)

            all_depots = {}
            if key_vdf_path:
                try:
                    all_depots = vdf.loads(zf.read(key_vdf_path).decode('utf-8')).get('depots', {})
                except Exception as e:
                    self.log.error(f"解析 key.vdf 失败: {e}")

            # 额外信息：config.json 里的 DLC 列表，用于日志提示
            config_path = next((n for n in names if n.endswith('config.json')), None)
            if config_path:
                try:
                    meta = json.loads(zf.read(config_path).decode('utf-8'))
                    if meta.get('dlcs'):
                        self.log.info(f"该 AppID 在仓库中登记的 DLC: {meta.get('dlcs')}")
                except Exception:
                    pass

            if self._is_lua_unlocker(unlocker_type):
                self.log.info(f"{'OpenSteam' if unlocker_type == 'opensteam' else 'SteamTools'} 自动更新模式: {'已启用' if use_st_auto_update else '已禁用'}")
                lua_dir = self._lua_write_dir(unlocker_type)
                lua_dir.mkdir(parents=True, exist_ok=True)
                lua_filepath = lua_dir / f"{app_id}.lua"

                # 部分清单库（如 dc190/ManifestHub）分支里不带 key.vdf，只有一份现成的
                # <appid>.lua，其中 addappid(depot) 行没有密钥。这种情况下 depot 列表仍以
                # zip 内的 lua 为准，密钥再去密钥库里补，避免生成缺 key 的无效解锁文件。
                depot_ids_from_lua = []
                lua_in_zip = next((n for n in names if n.lower().endswith(f'{app_id}.lua')), None)
                if lua_in_zip and not all_depots:
                    try:
                        zip_lua_text = zf.read(lua_in_zip).decode('utf-8', errors='ignore')
                        depot_ids_from_lua = re.findall(r'addappid\(\s*(\d+)\s*\)', zip_lua_text)
                        depot_ids_from_lua = [d for d in depot_ids_from_lua if d != str(app_id)]
                        if depot_ids_from_lua:
                            self.log.info(f"分支内 lua 提供 {len(depot_ids_from_lua)} 个 depot，密钥将从密钥库补全。")
                    except Exception as e:
                        self.log.warning(f"读取分支内 lua 失败: {e}")

                # 需要补密钥的 depot 集合
                missing_key_depots = [d for d in depot_ids_from_lua]

                async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as lua_file:
                    await lua_file.write(f'addappid({app_id})\n')
                    for depot_id, info in all_depots.items():
                        key = (info or {}).get("DecryptionKey", "")
                        if key:
                            await lua_file.write(f'addappid({depot_id}, 1, "{key}")\n')

                    if missing_key_depots:
                        # 从缓存的密钥库（Sudama / ManifestHub depotkeys.json）补齐
                        try:
                            key_db = await self._get_cached_sudama_data()
                        except Exception as e:
                            self.log.warning(f"读取密钥库失败，跳过补 key: {e}")
                            key_db = {}
                        patched = 0
                        for depot_id in missing_key_depots:
                            key = key_db.get(str(depot_id)) or key_db.get(str(depot_id), "")
                            if key:
                                await lua_file.write(f'addappid({depot_id}, 1, "{key}")\n')
                                patched += 1
                            else:
                                await lua_file.write(f'addappid({depot_id})\n')
                        self.log.info(f"已补全 {patched}/{len(missing_key_depots)} 个 depotkey。")

                    for manifest_path in all_manifest_paths_in_tree:
                        match = re.search(r'(\d+)_(\w+)\.manifest', Path(manifest_path).name)
                        if match:
                            line = f'setManifestid({match.group(1)}, "{match.group(2)}")\n'
                            await lua_file.write('--' + line if use_st_auto_update else line)

                tool_label = "OpenSteam" if unlocker_type == "opensteam" else "SteamTools"
                self.log.info(f"已为{tool_label}生成解锁文件: {lua_filepath}")

                if add_all_dlc:
                    await self._add_free_dlcs_to_lua(app_id, lua_filepath)

                if patch_depot_key:
                    self.log.info("开始修补创意工坊depotkey...")
                    await self.patch_lua_with_depotkey(app_id, lua_filepath)
            else:
                self.log.info("检测到 GreenLuma/标准模式，将复制 .manifest 文件到 depotcache。")
                if not all_manifest_paths_in_tree:
                    self.log.error("GreenLuma 模式需要 .manifest 文件，但 zip 中未找到。")
                    return False
                depot_cache_path = self.steam_path / 'depotcache'
                depot_cache_path.mkdir(parents=True, exist_ok=True)
                for manifest_path in all_manifest_paths_in_tree:
                    filename = Path(manifest_path).name
                    (depot_cache_path / filename).write_bytes(zf.read(manifest_path))
                    self.log.info(f"已为 GreenLuma 保存清单: {filename}")

                if all_depots:
                    await self.depotkey_merge(self.steam_path / 'config' / 'config.vdf', {'depots': all_depots})
                    gl_ids = list(all_depots.keys())
                    gl_ids.append(app_id)
                    await self.greenluma_add(list(set(gl_ids)))
                    self.log.info("已合并密钥并添加到GreenLuma。")

            return True

        except Exception as e:
            self.log.error(f"通过 codeload 处理 {repo} 的 {app_id} 时出错: {self.stack_error(e)}")
            return False

    async def process_github_manifest(self, app_id: str, repo: str, unlocker_type: str, use_st_auto_update: bool, add_all_dlc: bool, patch_depot_key: bool = False) -> bool:
        github_token = self.config.get("Github_Personal_Token", "")
        headers = {'Authorization': f'Bearer {github_token}'} if github_token else None
        
        url = f'https://api.github.com/repos/{repo}/branches/{app_id}'
        r_json = await self.fetch_branch_info(url, headers)
        if not (r_json and 'commit' in r_json):
            # API 走不通（分支不存在 / 配额耗尽 / 网络问题）时，改用 codeload 直接拉分支 zip：
            # codeload.github.com 不消耗 GitHub API 配额，也不受 60 次/小时限制。
            self.log.warning(f'无法通过 GitHub API 获取 {repo} 中 {app_id} 的分支信息，改用 codeload 免配额通道重试...')
            return await self._process_github_repo_via_codeload(
                app_id, repo, unlocker_type, use_st_auto_update, add_all_dlc, patch_depot_key
            )
        
        sha, tree_url = r_json['commit']['sha'], r_json['commit']['commit']['tree']['url']
        r2_json = await self.fetch_branch_info(tree_url, headers)
        if not (r2_json and 'tree' in r2_json):
            self.log.error(f'无法获取 {repo} 中 {app_id} 的文件列表。')
            return False
            
        all_files_in_tree = r2_json.get('tree', [])
        files_to_download = all_files_in_tree[:]
        
        if self._is_lua_unlocker(unlocker_type) and use_st_auto_update:
            files_to_download = [item for item in all_files_in_tree if not item['path'].endswith('.manifest')]
        
        if not files_to_download and all_files_in_tree: self.log.info("没有需要下载的文件（可能是因为自动更新模式跳过了所有文件）。")
        if not all_files_in_tree:
            self.log.warning(f"仓库 {repo} 的分支 {app_id} 为空。")
            return True

        try:
            downloaded_files = {}
            if files_to_download:
                tasks = [self._get_from_mirrors(sha, item['path'], repo) for item in files_to_download]
                downloaded_contents = await asyncio.gather(*tasks)
                downloaded_files = {item['path']: content for item, content in zip(files_to_download, downloaded_contents)}
        except Exception as e:
            self.log.error(f"下载文件失败，正在中止对 {app_id} 的处理: {e}")
            return False
        
        all_manifest_paths_in_tree = [item['path'] for item in all_files_in_tree if item['path'].endswith('.manifest')]
        downloaded_manifest_paths = [p for p in downloaded_files if p.endswith('.manifest')]
        key_vdf_path = next((p for p in downloaded_files if "key.vdf" in p.lower()), None)
        all_depots = {}
        if key_vdf_path:
            try:
                depots_config = vdf.loads(downloaded_files[key_vdf_path].decode('utf-8'))
                all_depots = depots_config.get('depots', {})
            except Exception as e: self.log.error(f"解析 key.vdf 失败: {e}")

        if self._is_lua_unlocker(unlocker_type):
            self.log.info(f"{'OpenSteam' if unlocker_type == 'opensteam' else 'SteamTools'} 自动更新模式: {'已启用' if use_st_auto_update else '已禁用'}")
            stplug_path = self._lua_write_dir(unlocker_type)
            stplug_path.mkdir(parents=True, exist_ok=True)
            lua_filename = f"{app_id}.lua"
            lua_filepath = stplug_path / lua_filename

            # 部分清单库（如 dc190/ManifestHub）分支里没有 key.vdf，只有一份
            # <appid>.lua，其中 addappid(depot) 行不带密钥（对应 config 里 has_key: false）。
            # 这类库里 depot 列表仍以 zip 内的 lua 为准，密钥去密钥库补，
            # 否则会写出缺 key、无法解锁的 lua。
            depot_ids_from_lua = []
            lua_in_tree = next((p for p in downloaded_files if p.lower().endswith(f'{app_id}.lua')), None)
            if lua_in_tree and not all_depots:
                try:
                    zip_lua_text = downloaded_files[lua_in_tree].decode('utf-8', errors='ignore')
                    depot_ids_from_lua = re.findall(r'addappid\(\s*(\d+)\s*\)', zip_lua_text)
                    depot_ids_from_lua = [d for d in depot_ids_from_lua if d != str(app_id)]
                    if depot_ids_from_lua:
                        self.log.info(f"分支内 lua 提供 {len(depot_ids_from_lua)} 个 depot，密钥将从密钥库补全。")
                except Exception as e:
                    self.log.warning(f"读取分支内 lua 失败: {e}")

            async with aiofiles.open(lua_filepath, mode="w", encoding="utf-8") as lua_file:
                await lua_file.write(f'addappid({app_id})\n')
                for depot_id, info in all_depots.items():
                    key = info.get("DecryptionKey", "")
                    await lua_file.write(f'addappid({depot_id}, 1, "{key}")\n')

                if depot_ids_from_lua:
                    try:
                        key_db = await self._get_cached_sudama_data()
                    except Exception as e:
                        self.log.warning(f"读取密钥库失败，跳过补 key: {e}")
                        key_db = {}
                    patched = 0
                    for depot_id in depot_ids_from_lua:
                        key = key_db.get(str(depot_id), "")
                        if key:
                            await lua_file.write(f'addappid({depot_id}, 1, "{key}")\n')
                            patched += 1
                        else:
                            await lua_file.write(f'addappid({depot_id})\n')
                    self.log.info(f"已补全 {patched}/{len(depot_ids_from_lua)} 个 depotkey。")

                for manifest_file_path in all_manifest_paths_in_tree:
                    match = re.search(r'(\d+)_(\w+)\.manifest', Path(manifest_file_path).name)
                    if match:
                        line = f'setManifestid({match.group(1)}, "{match.group(2)}")\n'
                        if use_st_auto_update: await lua_file.write('--' + line)
                        else: await lua_file.write(line)
            self.log.info(f"已为 {unlocker_type.capitalize()} 生成解锁文件: {app_id}.lua")
            
            if add_all_dlc:
                await self._add_free_dlcs_to_lua(app_id, lua_filepath)

            # NEW: Apply depotkey patch if requested
            if patch_depot_key:
                self.log.info("开始修补创意工坊depotkey...")
                await self.patch_lua_with_depotkey(app_id, lua_filepath)

        else:
            self.log.info("检测到 GreenLuma/标准模式，将复制 .manifest 文件到 depotcache。")
            if not downloaded_manifest_paths:
                self.log.error("GreenLuma 模式需要 .manifest 文件，但未能找到或下载。")
                return False
            
            depot_cache_path = self.steam_path / 'depotcache'
            for path in downloaded_manifest_paths:
                filename = Path(path).name
                (depot_cache_path / filename).write_bytes(downloaded_files[path])
                self.log.info(f"已为 GreenLuma 保存清单: {filename}")
            
            if all_depots:
                await self.depotkey_merge(self.steam_path / 'config' / 'config.vdf', {'depots': all_depots})
                gl_ids = list(all_depots.keys())
                gl_ids.append(app_id)
                await self.greenluma_add(list(set(gl_ids)))
                self.log.info("已合并密钥并添加到GreenLuma。")

        self.log.info(f'清单最后更新时间: {r_json["commit"]["commit"]["author"]["date"]}')
        return True
    
    def extract_app_id(self, user_input: str) -> str | None:
        match = re.search(r"/app/(\d+)", user_input) or re.search(r"steamdb\.info/app/(\d+)", user_input)
        if match: return match.group(1)
        return user_input if user_input.isdigit() else None

    async def find_appid_by_name(self, game_name: str) -> List[Dict]:
        """按名称搜索游戏，返回 [{appid, name, header_image}]。

        使用 Steam 商店的 storesearch 接口，中英文都能命中；先按简体中文区搜一次，
        结果为空时再按英文搜一次，覆盖只登记了英文名的作品。
        """
        game_name = (game_name or "").strip()
        if not game_name:
            return []

        # 直接输入 AppID 时不必走搜索
        if game_name.isdigit():
            name = await self._fetch_game_name_for_manager(game_name)
            return [{'appid': game_name, 'name': name or game_name, 'header_image': ''}]

        self.log.info(f"正在尝试搜索游戏: {game_name}")

        url = 'https://store.steampowered.com/api/storesearch/'
        headers = {'User-Agent': 'Mozilla/5.0'}

        try:
            for lang, cc in (('schinese', 'cn'), ('english', 'us')):
                try:
                    r = await self.client.get(
                        url,
                        params={'term': game_name, 'l': lang, 'cc': cc},
                        headers=headers,
                        timeout=20,
                    )
                    if r.status_code == 403:
                        self.log.warning("搜索接口被拦截 (403)，请稍后重试。")
                        continue
                    r.raise_for_status()
                    payload = r.json()
                except Exception as e:
                    self.log.debug(f"storesearch({lang}) 请求失败: {type(e).__name__}")
                    continue

                items = payload.get('items') if isinstance(payload, dict) else None
                if not isinstance(items, list) or not items:
                    continue

                games_list = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    appid = item.get('id')
                    name = item.get('name')
                    if appid and name:
                        games_list.append({
                            'appid': str(appid),
                            'name': str(name),
                            'header_image': item.get('tiny_image') or '',
                        })

                if games_list:
                    self.log.info(f"成功找到 {len(games_list)} 个结果")
                    return games_list

            self.log.warning("未找到相关游戏。")
        except Exception as e:
            self.log.error(f"搜索游戏 '{game_name}' 失败: {self.stack_error(e)}")

        return []

    async def cleanup_temp_files(self):
        try:
            if self.temp_path.exists():
                shutil.rmtree(self.temp_path)
                self.log.info('临时文件已清理。')
        except Exception as e:
            self.log.error(f'清理临时文件失败: {self.stack_error(e)}')

    async def migrate(self, st_use: bool):
        directory = self.steam_path / "config" / "stplug-in"
        if st_use and directory.exists():
            self.log.info('检测到SteamTools, 正在检查是否有旧文件需要迁移...')
            for file in directory.glob("Cai_unlock_*.lua"):
                new_filename = directory / file.name.replace("Cai_unlock_", "")
                try:
                    file.rename(new_filename)
                    self.log.info(f'已重命名: {file.name} -> {new_filename.name}')
                except Exception as e:
                    self.log.error(f'重命名失败 {file.name}: {e}')