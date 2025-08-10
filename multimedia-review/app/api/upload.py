"""
文件上传API路由
提供文件上传、状态查询等接口
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, File, UploadFile, Form, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi.responses import Response
import csv
import json
from io import StringIO
from datetime import datetime
from pathlib import Path
from sqlalchemy import desc, asc, func

from app.database import get_db
from app.services.file_service import FileService
from app.services.queue_service import QueueService
from app.models.file import FileType, FileStatus, ReviewFile
from app.utils.response import APIResponse, ValidationError
from app.config import get_settings
from app.models.task import ReviewTask
from app.models.result import ReviewResult, ViolationResult

# 创建路由器
router = APIRouter()


# 响应模型定义
class UploadResponse(BaseModel):
    """上传响应模型"""
    file_id: str
    original_name: str
    file_type: str
    file_size: int
    status: str
    message: str


class BatchUploadResponse(BaseModel):
    """批量上传响应模型"""
    success_count: int
    failed_count: int
    success_files: List[UploadResponse]
    failed_files: List[dict]


@router.post("/single", summary="单文件上传")
async def upload_single_file(
    task_id: str = Form(..., description="任务ID"),
    file: UploadFile = File(..., description="上传的文件"),
    db: Session = Depends(get_db)
):
    """
    上传单个文件到指定任务
    
    - **task_id**: 目标任务ID
    - **file**: 要上传的文件
    
    支持的文件类型：
    - 文档：PDF, DOCX, DOC, TXT
    - 图片：JPG, JPEG, PNG, GIF, BMP
    - 视频：MP4, AVI, MOV, WMV, FLV
    """
    settings = get_settings()
    file_service = FileService(db)
    queue_service = QueueService()
    
    # 验证文件大小
    if file.size and file.size > settings.MAX_FILE_SIZE:
        raise ValidationError(
            f"文件大小超过限制 ({file.size//1024//1024}MB > "
            f"{settings.MAX_FILE_SIZE//1024//1024}MB)"
        )
    
    # 验证文件类型
    if file.filename:
        ext = Path(file.filename).suffix.lower().lstrip('.')
        if ext not in settings.ALLOWED_EXTENSIONS:
            raise ValidationError(f"不支持的文件类型: {ext}")
    
    try:
        # 读取文件内容
        file_content = await file.read()
        
        # 上传文件
        uploaded_file = file_service.upload_file(
            task_id=task_id,
            file_content=file_content,
            original_name=file.filename or "unknown"
        )
        
        # 添加到处理队列
        queue_service.add_file_to_queue(
            file_id=str(uploaded_file.id),
            task_id=task_id,
            file_type=uploaded_file.file_type.value
        )
        
        return APIResponse.success(
            data=UploadResponse(
                file_id=str(uploaded_file.id),
                original_name=uploaded_file.original_name,
                file_type=uploaded_file.file_type.value,
                file_size=uploaded_file.file_size,
                status=uploaded_file.status.value,
                message="文件上传成功"
            ),
            message="文件上传成功"
        )
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件上传失败: {str(e)}")


@router.post("/batch", summary="批量文件上传")
async def upload_batch_files(
    task_id: str = Form(..., description="任务ID"),
    files: List[UploadFile] = File(..., description="上传的文件列表"),
    db: Session = Depends(get_db)
):
    """
    批量上传文件到指定任务
    
    - **task_id**: 目标任务ID
    - **files**: 要上传的文件列表（最多20个）
    """
    settings = get_settings()
    
    # 限制批量上传数量
    if len(files) > 20:
        raise ValidationError("批量上传最多支持20个文件")
    
    file_service = FileService(db)
    queue_service = QueueService()
    
    success_files = []
    failed_files = []
    
    for file in files:
        try:
            # 验证单个文件
            if file.size and file.size > settings.MAX_FILE_SIZE:
                failed_files.append({
                    "name": file.filename,
                    "error": f"文件大小超过限制 ({file.size//1024//1024}MB)"
                })
                continue
            
            if file.filename:
                
                ext = Path(file.filename).suffix.lower().lstrip('.')
                if ext not in settings.ALLOWED_EXTENSIONS:
                    failed_files.append({
                        "name": file.filename,
                        "error": f"不支持的文件类型: {ext}"
                    })
                    continue
            
            # 读取文件内容
            file_content = await file.read()
            
            # 上传文件
            uploaded_file = file_service.upload_file(
                task_id=task_id,
                file_content=file_content,
                original_name=file.filename or "unknown"
            )
            
            # 添加到处理队列
            queue_service.add_file_to_queue(
                file_id=str(uploaded_file.id),
                task_id=task_id,
                file_type=uploaded_file.file_type.value
            )
            
            success_files.append(UploadResponse(
                file_id=str(uploaded_file.id),
                original_name=uploaded_file.original_name,
                file_type=uploaded_file.file_type.value,
                file_size=uploaded_file.file_size,
                status=uploaded_file.status.value,
                message="上传成功"
            ))
        
        except Exception as e:
            failed_files.append({
                "name": file.filename,
                "error": str(e)
            })
    
    return APIResponse.success(
        data=BatchUploadResponse(
            success_count=len(success_files),
            failed_count=len(failed_files),
            success_files=success_files,
            failed_files=failed_files
        ),
        message=f"批量上传完成，成功{len(success_files)}个，失败{len(failed_files)}个"
    )


@router.get("/status/{file_id}", summary="查询文件处理状态")
async def get_file_status(
    file_id: str,
    db: Session = Depends(get_db)
):
    """
    查询文件处理状态和进度
    
    - **file_id**: 文件ID
    """
    file_service = FileService(db)
    queue_service = QueueService()
    
    # 获取文件信息
    file_obj = file_service.get_file_by_id(file_id)
    
    # 获取队列中的状态
    queue_status = queue_service.get_file_status(file_id)
    queue_progress = queue_service.get_progress(file_id)
    
    status_data = {
        "file_info": file_obj.to_dict(),
        "queue_status": queue_status,
        "queue_progress": queue_progress
    }
    
    return APIResponse.success(
        data=status_data,
        message="状态查询成功"
    )


@router.get("/status", summary="批量查询文件状态")
async def get_batch_file_status(
    file_ids: str = Query(..., description="文件ID列表，逗号分隔"),
    db: Session = Depends(get_db)
):
    """
    批量查询多个文件的处理状态
    
    - **file_ids**: 文件ID列表，使用逗号分隔，如：id1,id2,id3
    """
    file_service = FileService(db)
    queue_service = QueueService()
    
    # 解析文件ID列表
    id_list = [fid.strip() for fid in file_ids.split(',') if fid.strip()]
    
    if len(id_list) > 50:
        raise ValidationError("批量查询最多支持50个文件")
    
    status_list = []
    
    for file_id in id_list:
        try:
            file_obj = file_service.get_file_by_id(file_id)
            queue_status = queue_service.get_file_status(file_id)
            queue_progress = queue_service.get_progress(file_id)
            
            status_list.append({
                "file_id": file_id,
                "file_info": file_obj.to_dict(),
                "queue_status": queue_status,
                "queue_progress": queue_progress
            })
        
        except Exception as e:
            status_list.append({
                "file_id": file_id,
                "error": str(e)
            })
    
    return APIResponse.success(
        data=status_list,
        message="批量状态查询完成"
    )


@router.delete("/{file_id}", summary="删除文件")
async def delete_file(
    file_id: str,
    db: Session = Depends(get_db)
):
    """
    删除指定文件
    
    注意：正在处理中的文件不能删除
    """
    file_service = FileService(db)
    
    success = file_service.delete_file(file_id)
    
    return APIResponse.success(
        data={"deleted": success},
        message="文件删除成功"
    )


@router.get("/{file_id}/download", summary="下载文件")
async def download_file(
    file_id: str,
    db: Session = Depends(get_db)
):
    """
    下载原始文件
    """
    
    file_service = FileService(db)
    
    # 获取文件信息
    file_obj = file_service.get_file_by_id(file_id)
    
    # 读取文件内容
    file_content = file_service.get_file_content(file_id)
    
    # 返回文件响应
    return Response(
        content=file_content,
        media_type=file_obj.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={file_obj.original_name}"
        }
    )


@router.get("/queue/status", summary="获取上传队列状态")
async def get_queue_status():
    """
    获取文件处理队列状态
    
    返回队列中等待处理的文件数量等信息
    """
    queue_service = QueueService()
    
    status = queue_service.get_queue_status()
    
    return APIResponse.success(
        data=status,
        message="队列状态获取成功"
    )

@router.get("/video/{file_id}/frames-with-results", summary="获取视频帧及其审核结果")
async def get_video_frames_with_results(
    file_id: str,
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(10, ge=1, le=50, description="每页帧数"),
    has_violations: Optional[bool] = Query(None, description="只显示有违规的帧"),
    db: Session = Depends(get_db)
):
    """
    获取视频文件的帧列表及其对应的审核结果
    
    支持分页浏览和过滤
    """
    file_service = FileService(db)
    
    # 获取文件信息
    file_obj = file_service.get_file_by_id(file_id)
    
    if file_obj.file_type != FileType.VIDEO:
        raise ValidationError("只能获取视频文件的帧")
    
    # 获取所有审核结果
    query = db.query(ReviewResult).filter(ReviewResult.file_id == file_id)
    
    if has_violations:
        query = query.filter(ReviewResult.violation_result == ViolationResult.NON_COMPLIANT)
    
    # 按时间戳排序
    results = query.order_by(ReviewResult.timestamp.asc()).all()
    
    # 按帧组织数据
    frames_data = {}
    for result in results:
        frame_key = result.page_number or 0
        
        if frame_key not in frames_data:
            frame_info = result.position or {}
            frames_data[frame_key] = {
                "frame_number": frame_key,
                "timestamp": result.timestamp,
                "frame_info": frame_info,
                "violations": [],
                "has_violations": False
            }
        
        frames_data[frame_key]["violations"].append({
            "result_id": str(result.id),
            "violation_result": result.violation_result.value,
            "confidence_score": result.confidence_score,
            "evidence": result.evidence
        })
        
        if result.violation_result == ViolationResult.NON_COMPLIANT:
            frames_data[frame_key]["has_violations"] = True
    
    # 转换为列表并分页
    all_frames = sorted(frames_data.values(), key=lambda x: x["timestamp"])
    total = len(all_frames)
    
    start_idx = (page - 1) * size
    end_idx = start_idx + size
    page_frames = all_frames[start_idx:end_idx]
    
    # 添加下载链接
    for frame in page_frames:
        if frame["frame_info"].get("filename"):
            frame["download_url"] = f"/api/v1/upload/video/{file_id}/frame/{frame['frame_info']['filename']}"
    
    return APIResponse.paginated(
        items=page_frames,
        total=total,
        page=page,
        size=size,
        message="视频帧及审核结果获取成功"
    )

@router.get("/files", summary="查询所有文件列表")
async def get_all_files(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页大小"),
    file_type: Optional[FileType] = Query(None, description="文件类型过滤"),
    status: Optional[FileStatus] = Query(None, description="文件状态过滤"),
    original_name: Optional[str] = Query(None, description="文件名搜索（模糊匹配）"),
    creator_id: Optional[str] = Query(None, description="创建者过滤"),
    start_date: Optional[str] = Query(None, description="开始日期过滤 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期过滤 (YYYY-MM-DD)"),
    min_size: Optional[int] = Query(None, ge=0, description="最小文件大小（字节）"),
    max_size: Optional[int] = Query(None, ge=0, description="最大文件大小（字节）"),
    has_violations: Optional[bool] = Query(None, description="是否有违规内容"),
    order_by: str = Query("created_at", regex="^(created_at|file_size|processed_at|original_name)$", description="排序字段"),
    order_desc: bool = Query(True, description="是否降序排列"),
    db: Session = Depends(get_db)
):
    """
    查询所有文件列表，支持多种过滤条件
    
    - **page**: 页码，默认1
    - **size**: 每页大小，默认20，最大100
    - **file_type**: 文件类型过滤 (document/image/video/text)
    - **status**: 文件状态过滤 (pending/processing/completed/failed/cancelled)
    - **original_name**: 文件名模糊搜索
    - **creator_id**: 创建者ID过滤
    - **start_date**: 开始日期过滤
    - **end_date**: 结束日期过滤
    - **min_size**: 最小文件大小
    - **max_size**: 最大文件大小
    - **has_violations**: 是否有违规内容
    - **order_by**: 排序字段 (created_at/file_size/processed_at/original_name)
    - **order_desc**: 是否降序排列
    """
    
    file_service = FileService(db)
    
    # 构建查询
    query = db.query(ReviewFile)
    
    # 文件类型过滤
    if file_type:
        query = query.filter(ReviewFile.file_type == file_type)
    
    # 文件状态过滤
    if status:
        query = query.filter(ReviewFile.status == status)
    
    # 文件名模糊搜索
    if original_name and original_name.strip():
        search_term = f"%{original_name.strip()}%"
        query = query.filter(ReviewFile.original_name.ilike(search_term))
    
    # 创建者过滤（通过关联任务）
    if creator_id:
        query = query.join(ReviewTask).filter(ReviewTask.creator_id == creator_id)
    
    # 日期范围过滤
    if start_date:
        try:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(ReviewFile.created_at >= start_datetime)
        except ValueError:
            raise ValidationError("开始日期格式错误，请使用 YYYY-MM-DD 格式")
    
    if end_date:
        try:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
            # 结束日期包含当天全天
            end_datetime = end_datetime.replace(hour=23, minute=59, second=59)
            query = query.filter(ReviewFile.created_at <= end_datetime)
        except ValueError:
            raise ValidationError("结束日期格式错误，请使用 YYYY-MM-DD 格式")
    
    # 文件大小过滤
    if min_size is not None:
        query = query.filter(ReviewFile.file_size >= min_size)
    
    if max_size is not None:
        query = query.filter(ReviewFile.file_size <= max_size)
    
    # 是否有违规内容过滤
    if has_violations is not None:
        if has_violations:
            query = query.filter(ReviewFile.violation_count > 0)
        else:
            query = query.filter(ReviewFile.violation_count == 0)
    
    # 排序
    order_column = getattr(ReviewFile, order_by)
    if order_desc:
        query = query.order_by(desc(order_column))
    else:
        query = query.order_by(asc(order_column))
    
    # 获取总数量
    total = query.count()
    
    # 分页查询
    files = query.offset((page - 1) * size).limit(size).all()
    
    # 转换为字典格式，添加关联任务信息
    file_list = []
    for file_obj in files:
        file_dict = file_obj.to_dict()
        
        # 添加任务信息
        if file_obj.task:
            file_dict["task_info"] = {
                "id": str(file_obj.task.id),
                "name": file_obj.task.name,
                "status": file_obj.task.status.value,
                "creator_id": file_obj.task.creator_id
            }
        else:
            file_dict["task_info"] = None
        
        # 添加额外的计算字段
        file_dict["file_size_mb"] = file_obj.file_size_mb
        file_dict["has_violations"] = file_obj.violation_count > 0
        file_dict["file_exists"] = file_obj.exists
        
        file_list.append(file_dict)
    
    return APIResponse.paginated(
        items=file_list,
        total=total,
        page=page,
        size=size,
        message="文件列表查询成功"
    )


@router.get("/files/statistics", summary="获取文件统计信息")
async def get_files_statistics(
    file_type: Optional[FileType] = Query(None, description="文件类型过滤"),
    status: Optional[FileStatus] = Query(None, description="文件状态过滤"),
    creator_id: Optional[str] = Query(None, description="创建者过滤"),
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    db: Session = Depends(get_db)
):
    """
    获取文件统计信息
    
    支持与文件列表相同的过滤条件
    """
    
    # 构建基础查询
    query = db.query(ReviewFile)
    
    # 应用过滤条件（与上面的查询接口保持一致）
    if file_type:
        query = query.filter(ReviewFile.file_type == file_type)
    
    if status:
        query = query.filter(ReviewFile.status == status)
    
    if creator_id:
        query = query.join(ReviewTask).filter(ReviewTask.creator_id == creator_id)
    
    if start_date:
        try:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(ReviewFile.created_at >= start_datetime)
        except ValueError:
            raise ValidationError("开始日期格式错误")
    
    if end_date:
        try:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
            end_datetime = end_datetime.replace(hour=23, minute=59, second=59)
            query = query.filter(ReviewFile.created_at <= end_datetime)
        except ValueError:
            raise ValidationError("结束日期格式错误")
    
    # 统计查询
    
    # 文件类型统计
    type_stats = db.query(
        ReviewFile.file_type,
        func.count(ReviewFile.id).label('count'),
        func.sum(ReviewFile.file_size).label('total_size')
    ).filter(
        ReviewFile.id.in_(query.with_entities(ReviewFile.id))
    ).group_by(ReviewFile.file_type).all()
    
    # 文件状态统计
    status_stats = db.query(
        ReviewFile.status,
        func.count(ReviewFile.id).label('count')
    ).filter(
        ReviewFile.id.in_(query.with_entities(ReviewFile.id))
    ).group_by(ReviewFile.status).all()
    
    # 基础统计
    total_files = query.count()
    total_size = query.with_entities(func.sum(ReviewFile.file_size)).scalar() or 0
    avg_size = query.with_entities(func.avg(ReviewFile.file_size)).scalar() or 0
    
    # 违规文件统计
    violation_files = query.filter(ReviewFile.violation_count > 0).count()
    
    # 处理完成统计
    completed_files = query.filter(ReviewFile.status == FileStatus.COMPLETED).count()
    failed_files = query.filter(ReviewFile.status == FileStatus.FAILED).count()
    processing_files = query.filter(ReviewFile.status == FileStatus.PROCESSING).count()
    
    statistics = {
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
        "average_size_mb": round(avg_size / (1024 * 1024), 2) if avg_size else 0,
        "violation_files": violation_files,
        "violation_rate": round(violation_files / total_files * 100, 2) if total_files > 0 else 0,
        "completed_files": completed_files,
        "failed_files": failed_files,
        "processing_files": processing_files,
        "completion_rate": round(completed_files / total_files * 100, 2) if total_files > 0 else 0,
        "file_type_stats": {
            stat.file_type.value: {
                "count": stat.count,
                "total_size_mb": round(stat.total_size / (1024 * 1024), 2) if stat.total_size else 0
            }
            for stat in type_stats
        },
        "status_stats": {
            stat.status.value: stat.count
            for stat in status_stats
        }
    }
    
    return APIResponse.success(
        data=statistics,
        message="文件统计信息获取成功"
    )


@router.get("/files/export", summary="导出文件列表")
async def export_files_list(
    format: str = Query("csv", regex="^(csv|json)$", description="导出格式"),
    file_type: Optional[FileType] = Query(None, description="文件类型过滤"),
    status: Optional[FileStatus] = Query(None, description="文件状态过滤"),
    has_violations: Optional[bool] = Query(None, description="是否有违规内容"),
    limit: int = Query(1000, ge=1, le=10000, description="导出数量限制"),
    db: Session = Depends(get_db)
):
    """
    导出文件列表数据
    
    - **format**: 导出格式 (csv/json)
    - **limit**: 导出数量限制，最大10000
    """
    
    # 构建查询
    query = db.query(ReviewFile)
    
    if file_type:
        query = query.filter(ReviewFile.file_type == file_type)
    
    if status:
        query = query.filter(ReviewFile.status == status)
    
    if has_violations is not None:
        if has_violations:
            query = query.filter(ReviewFile.violation_count > 0)
        else:
            query = query.filter(ReviewFile.violation_count == 0)
    
    # 获取数据
    files = query.order_by(ReviewFile.created_at.desc()).limit(limit).all()
    
    if format == "csv":
        # CSV导出
        output = StringIO()
        writer = csv.writer(output)
        
        # 写入表头
        headers = [
            "ID", "原始文件名", "文件类型", "文件大小(MB)", "状态", 
            "进度", "违规数量", "创建时间", "处理完成时间", "任务名称"
        ]
        writer.writerow(headers)
        
        # 写入数据
        for file_obj in files:
            row = [
                str(file_obj.id),
                file_obj.original_name,
                file_obj.file_type.value,
                file_obj.file_size_mb,
                file_obj.status.value,
                file_obj.progress,
                file_obj.violation_count,
                file_obj.created_at.strftime("%Y-%m-%d %H:%M:%S") if file_obj.created_at else "",
                file_obj.processed_at.strftime("%Y-%m-%d %H:%M:%S") if file_obj.processed_at else "",
                file_obj.task.name if file_obj.task else ""
            ]
            writer.writerow(row)
        
        content = output.getvalue()
        output.close()
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"files_export_{timestamp}.csv"
        
        return Response(
            content=content.encode('utf-8-sig'),  # 使用 UTF-8 BOM 确保Excel正确显示中文
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    else:
        # JSON导出
        file_list = []
        for file_obj in files:
            file_dict = file_obj.to_dict()
            if file_obj.task:
                file_dict["task_name"] = file_obj.task.name
            file_list.append(file_dict)
        
        content = json.dumps(file_list, ensure_ascii=False, indent=2)
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"files_export_{timestamp}.json"
        
        return Response(
            content=content.encode('utf-8'),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )