# app/config.py
import logging
from pydantic_settings import BaseSettings
from pathlib import Path
import os
import sys # <--- 添加这一行


# --- 获取项目根目录 ---
# app/config.py -> app -> KuaishouAPI (项目根)
API_ROOT_PATH = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # --- Kuaishou Cookie (从环境变量加载，非常重要!) ---
    # 在启动 API 前，需要设置环境变量 KUAISHOU_COOKIE
    # 例如: export KUAISHOU_COOKIE='你的快手Cookie字符串' (Linux/macOS)
    kuaishou_cookie: str = ""

    # --- 路径配置 ---
    # 下载文件的根目录
    download_path: Path = API_ROOT_PATH / "downloaded_videos"
    # 临时文件目录 (用于 KS-Downloader 下载过程)
    temp_path: Path = API_ROOT_PATH / "temp_download"
    # API 服务自身的任务状态数据库路径 (如果需要持久化任务状态)
    api_task_db_path: Path = API_ROOT_PATH / "api_tasks.db"
    # 子模块 KS-Downloader 的内部数据库路径 (如果需要访问其记录或配置)
    # *** 确保路径与子模块实际位置一致 ***
    ks_internal_db_path: Path = API_ROOT_PATH / "submodules" / "ks_downloader" / "KS-Downloader.db"
    # 子模块 KS-Downloader 的源文件路径
    ks_source_path: Path = API_ROOT_PATH / "submodules" / "ks_downloader" / "source"

    # --- 日志和网络配置 ---
    log_level: str = "INFO"
    ks_timeout: int = 15 # 可以适当增加超时时间
    ks_max_retry: int = 2 # API 中重试次数建议谨慎
    ks_proxy: str | None = None # API 代理设置, e.g., "http://localhost:7890"

    # --- 下载和并发控制 ---
    ks_max_workers: int = 4 # 后台下载并发数
    ks_chunk_size: int = 2 * 1024 * 1024 # 2MB 下载块

    # --- KS-Downloader 功能开关 (如果需要从 API 控制) ---
    # ks_folder_mode: bool = False
    # ks_author_archive: bool = False
    # ks_data_record: bool = False # 是否让 KS 记录到它自己的 DB
    # ks_music_download: bool = False
    # ks_cover_download: str = "" # "JPEG", "WEBP", or ""

    # Pydantic Settings 配置类
    class Config:
        env_file = API_ROOT_PATH / '.env' # 指定 .env 文件路径
        env_file_encoding = 'utf-8'
        case_sensitive = False # 环境变量名不区分大小写

# 创建全局配置实例
try:
    settings = Settings()
    # --- 确保目录存在 ---
    settings.download_path.mkdir(parents=True, exist_ok=True)
    settings.temp_path.mkdir(parents=True, exist_ok=True)
    logging.info("API 配置加载成功。")
    if not settings.kuaishou_cookie:
        logging.warning("环境变量 'KUAISHOU_COOKIE' 未设置，处理需要登录的视频会失败！")
except Exception as e:
    logging.error(f"加载 API 配置失败: {e}", exc_info=True)
    # 在实际应用中，配置加载失败应该阻止服务启动
    raise SystemExit(f"配置加载失败: {e}")

# --- 检查 KS Source 路径是否存在 ---
if not settings.ks_source_path.is_dir():
     logging.error(f"KS-Downloader source 目录未找到: {settings.ks_source_path}")
     raise SystemExit("请确认子模块路径配置正确且子模块已初始化。")

# --- 将 KS Source 添加到 sys.path ---
# （如果 main.py 中已添加，这里可以省略或作为备用）
if str(settings.ks_source_path) not in sys.path:
    sys.path.insert(0, str(settings.ks_source_path))
    logging.info(f"已将 {settings.ks_source_path} 添加到 sys.path (来自 config)")