# app/main.py (最终版本)

import asyncio
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Any, Dict, Optional

import uvicorn
from fastapi import (BackgroundTasks, Depends, FastAPI, HTTPException,
                   Path as FastApiPath)
from pydantic import BaseModel, Field, HttpUrl # Pydantic 用于数据校验和模型定义

# --- 路径处理：确保能找到子模块的 source ---
app_dir = Path(__file__).resolve().parent
project_root = app_dir.parent
# *** 确保 'submodules/ks_downloader' 与你步骤 2 中添加子模块的路径一致 ***
submodule_source_path = project_root / 'submodules' / 'ks_downloader' / 'source'

if not submodule_source_path.is_dir():
    logging.error(f"未找到 KS-Downloader 的 source 目录: {submodule_source_path}")
    raise SystemExit("请确认子模块已正确添加并初始化，且路径设置正确。")
else:
    if str(submodule_source_path) not in sys.path:
        sys.path.insert(0, str(submodule_source_path))
        logging.info(f"已将 {submodule_source_path} 添加到 sys.path")
# ---------------------------------------------

# --- 导入配置、服务和共享资源 ---
# 注意：这些导入应该在路径处理之后
try:
    from .config import settings # 导入 API 配置
    from .services import (KuaishouService, get_kuaishou_service, # 导入服务层和依赖注入工厂
                           shared_http_client, task_statuses) # 导入共享客户端和任务状态字典
except ImportError as e:
    logging.error(f"无法导入配置或服务: {e}", exc_info=True)
    logging.error("请确保 app/config.py 和 app/services.py 文件存在且无误。")
    raise SystemExit(f"启动失败，无法导入模块: {e}")
# ------------------------------------

# --- 日志配置 ---
logging.basicConfig(level=settings.log_level.upper(), format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("kuaishou_api_main")
# -----------------



class TargetAuthor(BaseModel):
    id: Optional[str] = ""
    sec_uid: Optional[str] = ""
    nickname: Optional[str] = ""
    avatar: Optional[str] = ""
    signature: Optional[str] = ""
    verified: Optional[bool] = False
    follower_count: Optional[int] = 0
    following_count: Optional[int] = 0
    region: Optional[str] = ""

class TargetStatistics(BaseModel):
    like_count: Optional[int] = 0
    comment_count: Optional[int] = 0
    share_count: Optional[int] = 0
    collect_count: Optional[int] = 0
    play_count: Optional[int] = 0

class TargetMedia(BaseModel):
    cover_url: Optional[str] = ""
    # 注意：根据内容是视频还是图集，video_url 或 image_urls 可能只有一个有值
    video_url: Optional[str] = ""
    image_urls: Optional[List[str]] = [] # 添加图集 URL 列表
    duration: Optional[int] = 0 # 秒
    width: Optional[int] = 0
    height: Optional[int] = 0
    quality: Optional[str] = None

class TargetSchema(BaseModel): # 主模型
    platform: str = "kuaishou"
    video_id: Optional[str] = ""
    original_url: Optional[str] = ""
    title: Optional[str] = ""
    description: Optional[str] = ""
    content: Optional[str] = ""
    tags: Optional[List[str]] = []
    type: Optional[str] = "video" # 默认为 video
    author: TargetAuthor = Field(default_factory=TargetAuthor) # 使用默认工厂创建空对象
    statistics: TargetStatistics = Field(default_factory=TargetStatistics)
    media: TargetMedia = Field(default_factory=TargetMedia)
    publish_time: Optional[str] = None # YYYY-MM-DD_HH:MM:SS 格式
    update_time: Optional[Any] = None # API 未提供

class TargetApiResponse(BaseModel): # API 响应的顶层结构
    status: str = "success"
    message: str = "信息提取成功"
    data: TargetSchema # 嵌套目标 Schema 数据


# --- API 请求和响应模型 ---
# (可以移到单独的 app/models.py 文件中以保持整洁)
class KuaishouUrlRequest(BaseModel):
    url: str = Field(..., description="需要处理的快手链接 (分享链接或视频页链接)")

class VideoInfo(BaseModel):
    platform: str | None = None
    video_id: str | None = None
    original_url: str | None = None
    title: str | None = None
    description: str | None = None
    author_id: str | None = None
    author_nickname: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    play_count: int | None = None
    cover_url: str | None = None
    video_url: str | None = None # 注意：这可能是临时的或有签名的 URL
    image_urls: list[str] | None = None # 用于图集
    duration: int | None = None # 秒
    width: int | None = None
    height: int | None = None
    publish_time: str | None = None # 格式如 "YYYY-MM-DD_HH:MM:SS"

class InfoApiResponse(BaseModel):
    status: str = Field("success")
    message: str = Field("信息提取成功")
    video_info: VideoInfo

class DownloadApiResponse(BaseModel):
    status: str = Field("queued")
    message: str = Field("下载任务已加入后台队列")
    task_id: str

class StatusApiResponse(BaseModel):
    task_id: str
    status: str # e.g., queued, processing, completed, failed, not_found
    message: str | None = None
    result_path: str | None = None
# ---------------------------

# --- FastAPI 应用实例和 Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期中的资源"""
    # 应用启动时
    logger.info("API 服务启动中...")
    # 可以在这里预热数据库连接池等
    # shared_http_client 已经在 services.py 中创建
    app.state.http_client = shared_http_client # 将共享客户端放入 app state (可选)
    logger.info("共享 HTTP Client 已在 services 模块准备就绪。")
    yield # 服务运行阶段
    # 应用关闭时
    logger.info("API 服务关闭中...")
    if hasattr(app.state, 'http_client') and app.state.http_client:
        await app.state.http_client.aclose()
        logger.info("共享 HTTP Client 已关闭。")
    # 关闭数据库连接池等

app = FastAPI(
    title="快手下载器 API (Project A)",
    description="使用 KS-Downloader 子模块提供快手处理接口 (生产级优化版)。",
    version="0.1.0",
    lifespan=lifespan # 注册生命周期事件
)
# -----------------------------------

# --- API 端点实现 ---
@app.get("/", summary="根路径", tags=["General"])
async def read_root():
    """检查服务是否运行。"""
    return {"message": "Kuaishou API Service is running!"}

@app.post("/info", response_model=TargetApiResponse, summary="仅获取快手视频信息", tags=["Kuaishou"])
async def get_kuaishou_info_only(
    request: KuaishouUrlRequest,
    # 使用 Depends 获取 KuaishouService 实例，FastAPI 会自动处理其依赖 (如 http_client)
    service: KuaishouService = Depends(get_kuaishou_service)
):
    """接收快手 URL，调用服务层提取视频元数据。"""
    logger.info(f"收到 /info 请求, URL: {request.url}")
    try:
        # 调用服务层方法获取元数据
        metadata_dict = await service.get_video_metadata(request.url)
        # 使用 Pydantic 模型进行数据校验和格式化输出
        video_info_obj = TargetSchema(**metadata_dict)

        logger.warning(f"TargetApiResponse data is : 成功提取信息, {video_info_obj}")
        return TargetApiResponse(data=video_info_obj)
    except HTTPException as e:
        # 如果 Service 层抛出了 HTTPException，直接重新抛出
        logger.warning(f"/info 请求处理失败 (HTTPException): {e.detail}")
        raise e
    except Exception as e:
        # 捕获 Service 层可能未处理的其他异常
        logger.error(f"/info 请求处理时发生内部错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"提取信息时发生意外错误: {str(e)}")

@app.post("/download", response_model=DownloadApiResponse, status_code=202, summary="请求后台下载快手视频", tags=["Kuaishou"])
async def request_kuaishou_download(
    request: KuaishouUrlRequest,
    background_tasks: BackgroundTasks, # FastAPI 自动注入后台任务管理器
    service: KuaishouService = Depends(get_kuaishou_service) # 注入服务实例
):
    """接收快手 URL，创建后台任务执行下载，并立即返回任务 ID。"""
    logger.info(f"收到 /download 请求, URL: {request.url}")
    task_id = str(uuid.uuid4()) # 生成唯一的任务 ID

    # 将实际下载操作添加到后台任务队列
    # service.perform_download 是一个 async 函数，FastAPI 会在后台执行它
    background_tasks.add_task(service.perform_download, request.url, task_id)
    logger.info(f"任务 [{task_id}] 已添加到后台队列，URL: {request.url}")

    # 立即返回 202 Accepted 响应和任务 ID
    return DownloadApiResponse(task_id=task_id)

@app.get("/download/status/{task_id}", response_model=StatusApiResponse, summary="查询后台下载任务状态", tags=["Kuaishou"])
async def get_download_status(
    # 从 URL 路径获取 task_id，并添加描述
    task_id: str = FastApiPath(..., description="要查询的任务 ID (UUID 格式)"),
    service: KuaishouService = Depends(get_kuaishou_service) # 注入服务实例
):
    """根据任务 ID 查询后台下载任务的状态。"""
    logger.info(f"收到 /download/status 请求, Task ID: {task_id}")
    # 调用服务层方法获取任务状态 (从内存字典或未来实现的数据库/缓存中获取)
    status_data = service.get_task_status(task_id)

    if status_data.get("status") == "not_found":
        logger.warning(f"查询的任务 ID 不存在: {task_id}")
        raise HTTPException(status_code=404, detail=f"任务 ID '{task_id}' 未找到")

    # 返回 Pydantic 模型对应的状态信息
    return StatusApiResponse(**status_data)
# --------------------

# # --- 用于直接运行此脚本进行本地测试 ---
# if __name__ == "__main__":
#     # 生产环境部署时，应使用 gunicorn + uvicorn workers，而不是直接运行这个脚本
#     logger.info("启动 API 服务器 (开发模式)...")
#     logger.info(f"API 文档请访问: http://127.0.0.1:8000/docs")
#     # 使用 uvicorn 运行 FastAPI 应用
#     # host="127.0.0.1" 只允许本机访问, 使用 "0.0.0.0" 允许局域网访问
#     # reload=True 方便开发时自动重载 (生产环境应为 False)
#     uvicorn.run(
#         "main:app", # 指向 app 目录下的 main.py 文件中的 app 实例
#         host="127.0.0.1",
#         port=9000,
#         reload=True, # 开发时开启自动重载
#         app_dir=str(app_dir), # 告诉 Uvicorn 应用所在的目录
#         log_level=settings.log_level.lower()
#     )
# -----------------------------------