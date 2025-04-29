# app/services.py (修正版)

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional
import uuid
from types import SimpleNamespace # 用于模拟 Manager

from fastapi import HTTPException # 用于在 Service 中抛出 API 异常
from httpx import AsyncClient # Service 将接收共享的 Client
import aiosqlite # 用于管理 API 自身的任务状态数据库 (如果需要)

# 导入 API 配置
from .config import settings

# --- 动态添加路径 ---
services_file_path = Path(__file__).resolve()
app_dir = services_file_path.parent
project_root = app_dir.parent # 项目根目录 (KuaishouAPI)
# *** 确保 'submodules/ks_downloader' 与你步骤 2 中添加子模块的路径一致 ***
submodule_root_path = project_root / 'submodules' / 'ks_downloader'

if not submodule_root_path.is_dir():
     logging.error(f"子模块根目录未找到: {submodule_root_path}")
     raise SystemExit("请确认子模块路径配置正确且子模块已初始化。")
else:
    # 添加子模块的根目录 (ks_downloader) 到 sys.path
    if str(submodule_root_path) not in sys.path:
        sys.path.insert(0, str(submodule_root_path))
        logging.info(f"已将子模块根目录 {submodule_root_path} 添加到 sys.path")
# -----------------------

# --- 按需导入 KS-Downloader 的组件 (使用带 source. 前缀) ---
try:
    from source.tools import Cleaner, base_client as ks_base_client
    from source.module import Database as KSDatabase
    from source.module import CacheError
    from source.link import Examiner, DetailPage
    from source.extract import HTMLExtractor
    from source.downloader import Downloader
except ImportError as e:
    logging.error(f"无法从子模块的 source 包导入 ({submodule_root_path / 'source'}) : {e}", exc_info=True)
    logging.error(f"当前的 sys.path: {sys.path}")
    raise SystemExit(f"Service 层导入失败: {e}")
# ------------------------------------------------------

logger = logging.getLogger(__name__)



# ====> 添加 MockConsole 类 <====
class MockConsole:
    """一个模拟 rich.console 的类，将调用转发到 logging 模块。"""
    def print(self, message, *args, **kwargs):
        # 将 print 映射到 info 级别
        logger.info(str(message))

    def info(self, message, *args, **kwargs):
        logger.info(str(message))

    def warning(self, message, *args, **kwargs):
        logger.warning(str(message))

    def error(self, message, *args, **kwargs):
        logger.error(str(message))

    # 可以根据需要添加 KS-Downloader 可能用到的其他 console 方法
    # 例如 input (虽然在 API 中不应该被调用)
    def input(self, prompt: str, *args, **kwargs) -> str:
        logger.warning(f"MockConsole.input 被意外调用: {prompt}")
        return "" # 返回一个默认值，防止阻塞

# --- 模拟任务状态存储 (简化版) ---
# 生产环境应替换为 Redis, 数据库或其他持久化存储
task_statuses: Dict[str, Dict[str, Any]] = {}
# ------------------------------------

class KuaishouService:
    """封装调用 KS-Downloader 核心逻辑的服务层"""

    def __init__(self, http_client: AsyncClient):
        """
        初始化服务，接收共享的 HTTP Client 和 API 配置。
        创建并配置 KS-Downloader 的核心组件实例。
        """
        self.http_client = http_client
        self.config = settings
        self.cleaner = Cleaner()

        # --- 创建一个模拟的 Manager 对象来传递给 KS 组件 ---
        self.mock_manager = SimpleNamespace(
            console=MockConsole(), # API 中不直接使用 Console
            client=self.http_client, # 关键：使用共享的 Client
            cleaner=self.cleaner,
            # ====> 使用文件顶部计算的 project_root <====
            root=project_root, # <--- 修正这里！不再使用 settings.API_ROOT_PATH
            # ==============================================
            path=self.config.download_path.parent, # KS Manager 的 work_path
            temp=self.config.temp_path, # 使用 API 配置的 temp 路径
            data=self.config.download_path.parent / "Data", # KS Manager 的 Data 路径
            folder=self.config.download_path, # KS Manager 的 folder (下载目标目录)
            timeout=self.config.ks_timeout,
            max_retry=self.config.ks_max_retry,
            proxy=self.config.ks_proxy,
            cookie=self.config.kuaishou_cookie, # 关键：注入 Cookie
            # 构造 Headers (需要提供User-Agent)
            pc_headers={"Cookie": settings.kuaishou_cookie, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"}, # 使用固定或配置的 UA
            pc_data_headers={"Cookie": settings.kuaishou_cookie, "User-Agent": "Mozilla/5.0...", "content-type": "application/json"},
            pc_download_headers={"User-Agent": "Mozilla/5.0..."},
            
            # pc_headers={"Cookie": settings.kuaishou_cookie, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"},
            # # 其他 Headers 暂时保持不变，如果需要可以参考 KS-Downloader source/variable/internal.py 中的 APP_HEADERS
            # pc_data_headers={"Cookie": settings.kuaishou_cookie, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1", "content-type": "application/json"}, # 如果调用 data API 也需要改
            # pc_download_headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"},
            
            
            # 其他参数
            name_format="发布日期 作者昵称 作品描述".split(), # 或从配置读取
            cover="", # 或从配置读取
            music=False, # 或从配置读取
            data_record=False, # 或从配置读取
            folder_mode=False, # 或从配置读取
            author_archive=False, # 或从配置读取
            chunk=self.config.ks_chunk_size,
            mapping_data={}, # 可选：从 KS DB 加载
            max_workers=self.config.ks_max_workers,
            filter_name=self.cleaner.filter_name # 提供方法引用
        )
        # 确保 manager 需要的目录存在
        self.mock_manager.temp.mkdir(exist_ok=True)
        self.mock_manager.data.mkdir(exist_ok=True)
        self.mock_manager.folder.mkdir(exist_ok=True)
        # ----------------------------------------------------

        # --- 初始化 KS 核心组件，传入模拟的 Manager ---
        try:
            self.examiner = Examiner(self.mock_manager)
            self.detail_page = DetailPage(self.mock_manager)
            self.html_extractor = HTMLExtractor(self.mock_manager)
            # --- Downloader 和 Database 按需初始化 ---
            # 如果需要记录到 KS 数据库 (可能需要适配 KSDatabase 的路径处理)
            # self.ks_database = KSDatabase(self.mock_manager)
            # Downloader 实例可以在 perform_download 中按需创建
            logger.info("KuaishouService 初始化完成，核心组件已创建")
        except Exception as e:
             logger.error(f"初始化 KuaishouService 组件时出错: {e}", exc_info=True)
             raise RuntimeError(f"Service 初始化失败: {e}")

    async def get_video_metadata(self, url: str) -> Dict[str, Any]:
        """获取视频元数据"""
        logger.warning(f"Service: 开始提取元数据 for URL: {url}")
        try:
            resolved_urls = await self.examiner.run(url, type_="detail")
            if not resolved_urls:
                raise ValueError("无法提取有效的快手作品链接")

            logger.warning(f"Service: 获取到的 resolved_urls: {resolved_urls}")

            target_url = resolved_urls[0]
            logger.warning(f"Service: 解析得到 URL: {target_url}")
            web, user_id, detail_id = self.examiner.extract_params(target_url)
            if not detail_id:
                raise ValueError("无法解析出作品 ID")
            logger.warning(f"web: {web}, user_id: {user_id}, detail_id: {detail_id}")

            html_content = await self.detail_page.run(target_url)
            if not html_content:
                logger.warning(f"Service: 获取 HTML 页面内容失败 for {target_url}")
                raise HTTPException(status_code=503, detail="获取快手页面内容失败，请检查网络或 Cookie")

            logger.warning(f"Service: 获取到的 HTML 内容 (前 1000 字符): {html_content[:1000]}")

            extracted_info = self.html_extractor.run(html_content, detail_id, web)
            if not extracted_info:
                 raise ValueError("从 HTML 中提取视频信息失败")

            logger.info(f"Service: 成功提取信息, 作品ID: {extracted_info.get('detailID')}")
            video_info_data = self._map_extracted_to_dict(extracted_info, url)
            return video_info_data

        except ValueError as e:
             # ====> 修改这里 <====
             # 使用 repr(e) 获取更安全的错误表示形式用于日志记录
             logger.error(f"Service: 处理链接或数据时值错误: {repr(e)}", exc_info=True)
             # 返回一个通用的、安全的错误信息给 API 调用者
             raise HTTPException(status_code=400, detail="处理输入链接或解析数据时发生值错误，请检查链接格式或内容。")
        except HTTPException:
             raise
        except Exception as e:
            logger.error(f"Service: 提取元数据时发生未知错误: {e}", exc_info=True)
            error_msg = str(e)
            if "Cookie" in error_msg or "登录" in error_msg:
                 raise HTTPException(status_code=401, detail=f"提取失败，可能需要有效 Cookie: {error_msg}")
            raise HTTPException(status_code=500, detail=f"提取元数据时发生内部错误: {error_msg}")

    # app/services.py -> 在 KuaishouService 类定义内部添加这个方法


    async def perform_download(self, url: str, task_id: str):
        """执行下载（在后台任务中运行）"""
        global task_statuses
        task_statuses[task_id] = {"status": "processing", "message": "初始化下载...", "result_path": None}
        logger.info(f"后台任务 [{task_id}] 开始下载: {url}")

        downloader_instance = None
        ks_db_instance = None

        try:
            logger.info(f"[{task_id}] 获取下载所需信息...")
            resolved_urls = await self.examiner.run(url, type_="detail")
            if not resolved_urls: raise ValueError("无法提取有效链接")
            target_url = resolved_urls[0]
            web, user_id, detail_id = self.examiner.extract_params(target_url)
            if not detail_id: raise ValueError("无法解析作品 ID")
            html_content = await self.detail_page.run(target_url)
            if not html_content: raise ValueError("获取 HTML 失败")
            extracted_info = self.html_extractor.run(html_content, detail_id, web)
            if not extracted_info: raise ValueError("提取信息失败")
            download_list_data = [extracted_info]

            logger.info(f"[{task_id}] 准备下载器...")
            # --- 实例化 Downloader ---
            # 注意：可能需要适配 KSDatabase 或传入 None
            downloader = Downloader(self.mock_manager, None) # 假设不使用 KS 数据库记录

            task_statuses[task_id]["message"] = "开始下载文件..."
            logger.info(f"[{task_id}] 调用 Downloader.run ...")

            # --- 调用并适配 Downloader.run ---
            # 你需要修改 Downloader.run 或其调用的内部方法：
            # 1. 不要直接打印进度，而是通过回调或其他方式更新 task_statuses[task_id]['progress']
            # 2. 让它返回最终下载成功的文件路径列表
            # 3. 确保它使用 self.mock_manager 中的路径和配置
            # 4. 移除或重定向其内部的 console 输出

            # 示例：假设 downloader.run 被修改为返回路径列表
            # result_paths = await downloader.run(download_list_data)
            # final_path = result_paths[0] if result_paths else None

            # --- 临时模拟结果 (需要替换为实际调用和结果处理) ---
            logger.warning(f"[{task_id}] 下载逻辑需要适配 KSDowloader.Downloader 类，此处为模拟。")
            await asyncio.sleep(5)
            filename_base = f"{extracted_info.get('timestamp', 'unk_time')}_{extracted_info.get('name', 'unk_author')}_{extracted_info.get('caption', 'unk_caption')}"
            safe_filename = self.cleaner.filter_name(filename_base, default=detail_id or uuid.uuid4().hex[:8])
            # 模拟一个最终路径 (真实路径由 Downloader 决定)
            final_path = self.mock_manager.folder / f"{safe_filename}.mp4" # 假设 mp4
            final_path.touch() # 模拟创建文件
            logger.info(f"[{task_id}] 模拟下载完成，路径: {final_path}")
            task_statuses[task_id] = {"status": "completed", "message": "下载成功 (模拟)", "result_path": str(final_path)}
            # ---------------------------------------------------

        except Exception as e:
            logger.error(f"后台任务 [{task_id}] 下载失败: {e}", exc_info=True)
            error_msg = str(e)
            if "Cookie" in error_msg or "登录" in error_msg:
                 task_statuses[task_id] = {"status": "failed", "message": f"下载失败，可能需要有效 Cookie: {error_msg}", "result_path": None}
            else:
                 task_statuses[task_id] = {"status": "failed", "message": error_msg, "result_path": None}
        finally:
            # 如果初始化了 KS 数据库，确保关闭
            if ks_db_instance:
                await ks_db_instance.close()

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """获取后台任务状态"""
        return task_statuses.get(task_id, {"status": "not_found", "message": "任务 ID 不存在"})

    # app/services.py -> KuaishouService 类内部

    def _map_extracted_to_dict(self, extracted_info: dict, original_url: str) -> dict:
        """
        将 KS Extractor 的字典映射到目标 schema 结构 (字典形式)。
        """
        if not extracted_info:
            return {} # 如果输入为空，返回空字典

        # --- 推断内容类型 ---
        media_type = "video" # 默认视频
        image_urls = []
        video_url = ""
        downloads = extracted_info.get("download", [])
        photo_type = extracted_info.get("photoType") # 假设提取器返回了类型 "视频" 或 "图片"
        if photo_type == "图片" and downloads:
            media_type = "image"
            image_urls = downloads
        elif photo_type == "视频" and downloads:
            video_url = downloads[0]
        # --------------------

        # --- 解析计数字段 ---
        parsed_like_count = self._parse_count_with_unit(extracted_info.get("realLikeCount"))
        parsed_comment_count = self._parse_count_with_unit(extracted_info.get("commentCount"))
        parsed_share_count = self._parse_count_with_unit(extracted_info.get("shareCount"))
        parsed_play_count = self._parse_count_with_unit(extracted_info.get("viewCount"))
        # --------------------

        target_schema_dict = {
            "platform": "kuaishou",
            "video_id": extracted_info.get("detailID", ""),
            "original_url": original_url,
            "title": extracted_info.get("caption", ""),
            "description": extracted_info.get("caption", ""), # 使用 caption 作为描述
            "content": "", # 保持为空
            "tags": [], # 保持为空
            "type": media_type, # 使用推断的类型
            "author": {
                "id": extracted_info.get("authorID", ""),
                "sec_uid": "",
                "nickname": extracted_info.get("name", ""),
                "avatar": "", # 需检查 extracted_info 是否包含头像信息，如 headUrls
                "signature": "",
                "verified": False,
                "follower_count": 0,
                "following_count": 0,
                "region": ""
            },
            "statistics": {
                "like_count": parsed_like_count if parsed_like_count is not None else 0,
                "comment_count": parsed_comment_count if parsed_comment_count is not None else 0,
                "share_count": parsed_share_count if parsed_share_count is not None else 0,
                "collect_count": 0,
                "play_count": parsed_play_count if parsed_play_count is not None else 0,
            },
            "media": {
                "cover_url": extracted_info.get("coverUrl", ""),
                "video_url": video_url, # 如果是视频则有值
                "image_urls": image_urls, # 如果是图集则有值
                "duration": self._parse_duration(extracted_info.get("duration")) or 0,
                "width": extracted_info.get("width") or 0,
                "height": extracted_info.get("height") or 0,
                "quality": None
            },
            "publish_time": extracted_info.get("timestamp"), # 保持 YYYY-MM-DD_HH:MM:SS
            "update_time": None
        }
        # 可以在这里进行最后的数据清理（比如移除值为None的键，如果需要的话）
        return target_schema_dict

    # 确保 _parse_duration 方法也存在于类中
    def _parse_duration(self, duration_str: str | int) -> int | None:
        """将 'HH:MM:SS' 或毫秒 转换为 秒"""
        if isinstance(duration_str, int):
             if duration_str > 0: return duration_str // 1000
             else: return 0
        if isinstance(duration_str, str) and ':' in duration_str:
            try:
                parts = list(map(int, duration_str.split(':')))
                if len(parts) == 3:
                    h, m, s = parts; return h * 3600 + m * 60 + s
                elif len(parts) == 2:
                    m, s = parts; return m * 60 + s
                elif len(parts) == 1:
                    return parts[0]
            except ValueError: return None
        return None

    # 确保 _parse_count_with_unit 辅助方法也存在于类中
    def _parse_count_with_unit(self, count_str: Any) -> int | None:
        """辅助方法：将 '1.2万', '5亿', '1234' 等字符串解析为整数"""
        if isinstance(count_str, int):
            return count_str if count_str >= 0 else None
        if not isinstance(count_str, str):
            return None

        count_str = count_str.strip().lower()
        num_part = count_str
        multiplier = 1

        if '万' in count_str or 'w' in count_str:
            num_part = count_str.replace('万', '').replace('w', '').strip()
            multiplier = 10000
        elif '亿' in count_str or 'b' in count_str:
            num_part = count_str.replace('亿', '').replace('b', '').strip()
            multiplier = 100000000

        try:
            num = float(num_part)
            return int(num * multiplier)
        except (ValueError, TypeError):
            logger.warning(f"无法将计数字符串 '{count_str}' 解析为有效的数字。") # 假设 logger 可用
            return None

# --- 用于 FastAPI 依赖注入 ---
# 创建共享的 HTTP Client
try:
    shared_http_client = ks_base_client(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", # 示例 UA
        timeout=settings.ks_timeout,
        proxy=settings.ks_proxy
    )
    logger.info("共享 HTTP Client (shared_http_client) 已创建。")
except Exception as e:
    logger.error(f"创建 shared_http_client 时发生未知错误: {e}", exc_info=True)
    raise SystemExit("无法创建共享 HTTP Client")

# 创建服务实例的工厂函数
def get_kuaishou_service() -> KuaishouService:
    """FastAPI 依赖注入工厂函数"""
    try:
        if not shared_http_client:
             raise RuntimeError("共享 HTTP Client (shared_http_client) 未初始化！")
        return KuaishouService(http_client=shared_http_client)
    except Exception as e:
        logger.error(f"创建 KuaishouService 实例时出错: {e}", exc_info=True)
        raise RuntimeError(f"无法创建 KuaishouService: {e}")
# --------------------------