"""
审核结果API路由
提供审核结果查询、人工标注等接口
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Path, Body
from pydantic import BaseModel, Field, ValidationError

from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db
from app.models.result import ReviewResult, ViolationResult, SourceType
from app.models.file import ReviewFile
from app.utils.response import APIResponse, NotFoundError

# 创建路由器
router = APIRouter()


# 请求模型定义
class MarkResultRequest(BaseModel):
    """标记审核结果请求模型"""
    reviewer_id: str = Field(..., description="复审人员ID")
    review_result: str = Field(..., pattern="^(confirmed|rejected|modified)$", description="复审结果")  # 改为 pattern
    review_comment: Optional[str] = Field(None, max_length=500, description="复审备注")


class ResultQueryParams(BaseModel):
    """结果查询参数"""
    page: int = Field(1, ge=1, description="页码")
    size: int = Field(20, ge=1, le=100, description="每页大小")
    violation_result: Optional[ViolationResult] = Field(None, description="检测结果过滤")
    source_type: Optional[SourceType] = Field(None, description="来源类型过滤")
    is_reviewed: Optional[bool] = Field(None, description="是否已复审过滤")
    min_confidence: Optional[float] = Field(None, ge=0, le=1, description="最小置信度")
    file_type: Optional[str] = Field(None, description="文件类型过滤")


@router.get("/", summary="获取审核结果列表")
async def get_result_list(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页大小"),
    task_id: Optional[str] = Query(None, description="任务ID过滤"),
    file_id: Optional[str] = Query(None, description="文件ID过滤"),
    violation_result: Optional[ViolationResult] = Query(None, description="检测结果过滤"),
    source_type: Optional[SourceType] = Query(None, description="来源类型过滤"),
    is_reviewed: Optional[bool] = Query(None, description="是否已复审过滤"),
    min_confidence: Optional[float] = Query(None, ge=0, le=1, description="最小置信度"),
    needs_review: Optional[bool] = Query(None, description="是否需要人工复审"),
    db: Session = Depends(get_db)
):
    """分页查询审核结果列表"""
    query = db.query(ReviewResult)
    
    # 文件ID过滤
    if file_id:
        query = query.filter(ReviewResult.file_id == file_id)
    
    # 任务ID过滤（通过关联文件）
    if task_id:
        query = query.join(ReviewFile).filter(ReviewFile.task_id == task_id)
    
    # 检测结果过滤
    if violation_result:
        query = query.filter(ReviewResult.violation_result == violation_result)
    
    # 来源类型过滤
    if source_type:
        query = query.filter(ReviewResult.source_type == source_type)
    
    # 是否已复审过滤
    if is_reviewed is not None:
        query = query.filter(ReviewResult.is_reviewed == is_reviewed)
    
    # 最小置信度过滤
    if min_confidence is not None:
        query = query.filter(ReviewResult.confidence_score >= min_confidence)
    
    # 是否需要人工复审过滤
    if needs_review is not None:
        if needs_review:
            # 需要复审：不确定结果或低置信度不合规结果
            query = query.filter(
                ReviewResult.is_reviewed == False,
                db.or_(
                    ReviewResult.violation_result == ViolationResult.UNCERTAIN,
                    db.and_(
                        ReviewResult.violation_result == ViolationResult.NON_COMPLIANT,
                        ReviewResult.confidence_score < 0.6
                    )
                )
            )
        else:
            # 不需要复审：已复审或高置信度结果
            query = query.filter(
                db.or_(
                    ReviewResult.is_reviewed == True,
                    db.and_(
                        ReviewResult.violation_result == ViolationResult.COMPLIANT,
                        ReviewResult.confidence_score >= 0.6
                    ),
                    db.and_(
                        ReviewResult.violation_result == ViolationResult.NON_COMPLIANT,
                        ReviewResult.confidence_score >= 0.6
                    )
                )
            )
    
    # 获取总数量
    total = query.count()
    
    # 分页查询
    results = query.order_by(
        ReviewResult.created_at.desc()
    ).offset((page - 1) * size).limit(size).all()
    
    # 转换为字典格式
    result_list = [result.to_dict() for result in results]
    
    return APIResponse.paginated(
        items=result_list,
        total=total,
        page=page,
        size=size,
        message="查询成功"
    )

@router.get("/file/{file_id}", summary="获取文件的审核结果")
async def get_file_results(
    file_id: str = Path(..., description="文件ID"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(50, ge=1, le=100, description="每页大小"),
    violation_result: Optional[ViolationResult] = Query(None, description="检测结果过滤"),
    db: Session = Depends(get_db)
):
    """获取指定文件的所有审核结果"""
    query = db.query(ReviewResult).filter(ReviewResult.file_id == file_id)
    
    # 检测结果过滤
    if violation_result:
        query = query.filter(ReviewResult.violation_result == violation_result)
    
    # 获取总数量
    total = query.count()
    
    # 分页查询
    results = query.order_by(
        ReviewResult.confidence_score.desc()
    ).offset((page - 1) * size).limit(size).all()
    
    # 转换为字典格式，包含位置信息用于前端标注
    result_list = []
    for result in results:
        result_dict = result.to_dict()
        # 添加额外的前端展示信息
        result_dict["display_info"] = {
            "color": _get_result_color(result.violation_result),
            "should_highlight": result.confidence_score > 0.7 and result.violation_result == ViolationResult.NON_COMPLIANT
        }
        result_list.append(result_dict)
    
    return APIResponse.paginated(
        items=result_list,
        total=total,
        page=page,
        size=size,
        message="文件审核结果获取成功"
    )


@router.get("/task/{task_id}/summary", summary="获取任务审核结果摘要")
async def get_task_result_summary(
    task_id: str = Path(..., description="任务ID"),
    db: Session = Depends(get_db)
):
    """获取任务的审核结果摘要统计"""
    # 检测结果统计
    result_stats = db.query(
        ReviewResult.violation_result,
        db.func.count(ReviewResult.id).label('count'),
        db.func.avg(ReviewResult.confidence_score).label('avg_confidence')
    ).join(ReviewFile).filter(
        ReviewFile.task_id == task_id
    ).group_by(ReviewResult.violation_result).all()
    
    # 来源类型统计
    source_stats = db.query(
        ReviewResult.source_type,
        db.func.count(ReviewResult.id).label('count')
    ).join(ReviewFile).filter(
        ReviewFile.task_id == task_id
    ).group_by(ReviewResult.source_type).all()
    
    # 人工复审统计
    review_stats = db.query(
        ReviewResult.is_reviewed,
        db.func.count(ReviewResult.id).label('count')
    ).join(ReviewFile).filter(
        ReviewFile.task_id == task_id
    ).group_by(ReviewResult.is_reviewed).all()
    
    # 需要复审的结果
    need_review_count = db.query(ReviewResult).join(ReviewFile).filter(
        ReviewFile.task_id == task_id,
        ReviewResult.is_reviewed == False,
        db.or_(
            ReviewResult.violation_result == ViolationResult.UNCERTAIN,
            db.and_(
                ReviewResult.violation_result == ViolationResult.NON_COMPLIANT,
                ReviewResult.confidence_score < 0.6
            )
        )
    ).count()
    
    # 总检测数量
    total_detections = db.query(ReviewResult).join(ReviewFile).filter(
        ReviewFile.task_id == task_id
    ).count()
    
    # 不合规数量
    non_compliant_count = db.query(ReviewResult).join(ReviewFile).filter(
        ReviewFile.task_id == task_id,
        ReviewResult.violation_result == ViolationResult.NON_COMPLIANT
    ).count()
    
    summary = {
        "total_detections": total_detections,
        "non_compliant_count": non_compliant_count,
        "need_review_count": need_review_count,
        "result_stats": {
            stat.violation_result.value: {
                "count": stat.count,
                "avg_confidence": float(stat.avg_confidence) if stat.avg_confidence else 0
            }
            for stat in result_stats
        },
        "source_type_stats": {
            stat.source_type.value: stat.count
            for stat in source_stats
        },
        "review_stats": {
            "reviewed": sum([stat.count for stat in review_stats if stat.is_reviewed]),
            "unreviewed": sum([stat.count for stat in review_stats if not stat.is_reviewed])
        }
    }
    
    return APIResponse.success(
        data=summary,
        message="任务审核结果摘要获取成功"
    )

@router.get("/pending-review", summary="获取待复审结果列表")
async def get_pending_review_results(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页大小"),
    violation_result: Optional[ViolationResult] = Query(None, description="检测结果过滤"),
    priority: str = Query("confidence", regex="^(confidence|time|result)$", description="排序优先级"),
    db: Session = Depends(get_db)
):
    """获取需要人工复审的结果列表"""
    query = db.query(ReviewResult).filter(
        ReviewResult.is_reviewed == False,
        db.or_(
            ReviewResult.violation_result == ViolationResult.UNCERTAIN,
            db.and_(
                ReviewResult.violation_result == ViolationResult.NON_COMPLIANT,
                ReviewResult.confidence_score < 0.6
            )
        )
    )
    
    # 检测结果过滤
    if violation_result:
        query = query.filter(ReviewResult.violation_result == violation_result)
    
    # 排序
    if priority == "confidence":
        query = query.order_by(ReviewResult.confidence_score.asc())  # 低置信度优先
    elif priority == "time":
        query = query.order_by(ReviewResult.created_at.asc())  # 时间早的优先
    else:  # result
        query = query.order_by(ReviewResult.violation_result, ReviewResult.confidence_score.asc())
    
    # 获取总数量
    total = query.count()
    
    # 分页查询
    results = query.offset((page - 1) * size).limit(size).all()
    
    # 转换为字典格式
    result_list = [result.to_dict() for result in results]
    
    return APIResponse.paginated(
        items=result_list,
        total=total,
        page=page,
        size=size,
        message="待复审结果获取成功"
    )



@router.get("/{result_id}", summary="获取审核结果详情")
async def get_result_detail(
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    根据ID获取审核结果详细信息
    """
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    return APIResponse.success(
        data=result.to_dict(),
        message="查询成功"
    )


@router.post("/{result_id}/mark", summary="人工标记审核结果")
async def mark_result(
    request: MarkResultRequest,
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    人工标记/修改审核结果
    
    - **reviewer_id**: 复审人员ID
    - **review_result**: 复审结果 (confirmed/rejected/modified)
    - **review_comment**: 复审备注（可选）
    """
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    # 标记为已复审
    result.mark_reviewed(
        reviewer_id=request.reviewer_id,
        result=request.review_result,
        comment=request.review_comment
    )
    
    db.commit()
    db.refresh(result)
    
    return APIResponse.success(
        data=result.to_dict(),
        message="审核结果标记成功"
    )


def _get_result_color(violation_result: ViolationResult) -> str:
    """获取检测结果对应的颜色"""
    color_map = {
        ViolationResult.COMPLIANT: "#52c41a",      # 绿色 - 合规
        ViolationResult.NON_COMPLIANT: "#ff4d4f",  # 红色 - 不合规
        ViolationResult.UNCERTAIN: "#faad14"       # 橙色 - 不确定
    }
    return color_map.get(violation_result, "#d9d9d9")


@router.delete("/{result_id}", summary="删除审核结果")
async def delete_result(
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    删除指定的审核结果记录
    
    - **result_id**: 要删除的结果ID
    """
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    # 保存文件ID用于更新统计
    file_id = str(result.file_id)
    
    # 删除结果记录
    db.delete(result)
    db.commit()
    
    # 更新文件的违规统计
    from app.services.file_service import FileService
    file_service = FileService(db)
    try:
        file_service.update_file_violation_count(file_id)
    except Exception as e:
        logger.warning(f"更新文件统计失败: {e}")
    
    return APIResponse.success(
        data={"deleted": True, "result_id": result_id},
        message="审核结果删除成功"
    )


@router.delete("/batch", summary="批量删除审核结果")
async def batch_delete_results(
    result_ids: List[str] = Body(..., description="要删除的结果ID列表"),
    db: Session = Depends(get_db)
):
    """
    批量删除审核结果记录
    
    - **result_ids**: 要删除的结果ID列表（最多50个）
    """
    if len(result_ids) > 50:
        raise ValidationError("批量删除最多支持50个结果")
    
    if not result_ids:
        raise ValidationError("删除列表不能为空")
    
    # 查找存在的结果
    results = db.query(ReviewResult).filter(
        ReviewResult.id.in_(result_ids)
    ).all()
    
    if not results:
        raise NotFoundError("没有找到要删除的审核结果")
    
    deleted_ids = []
    not_found_ids = []
    affected_files = set()
    
    # 检查哪些ID存在
    found_ids = {str(result.id) for result in results}
    for result_id in result_ids:
        if result_id in found_ids:
            deleted_ids.append(result_id)
        else:
            not_found_ids.append(result_id)
    
    # 收集受影响的文件ID
    for result in results:
        affected_files.add(str(result.file_id))
    
    # 批量删除
    delete_count = db.query(ReviewResult).filter(
        ReviewResult.id.in_(deleted_ids)
    ).delete(synchronize_session=False)
    
    db.commit()
    
    # 更新受影响文件的统计
    from app.services.file_service import FileService
    file_service = FileService(db)
    for file_id in affected_files:
        try:
            file_service.update_file_violation_count(file_id)
        except Exception as e:
            logger.warning(f"更新文件{file_id}统计失败: {e}")
    
    return APIResponse.success(
        data={
            "deleted_count": delete_count,
            "deleted_ids": deleted_ids,
            "not_found_ids": not_found_ids,
            "affected_files": len(affected_files)
        },
        message=f"批量删除完成，成功删除{delete_count}个结果"
    )


@router.delete("/file/{file_id}/all", summary="删除文件的所有审核结果")
async def delete_file_all_results(
    file_id: str = Path(..., description="文件ID"),
    violation_result: Optional[ViolationResult] = Query(None, description="只删除指定结果类型"),
    db: Session = Depends(get_db)
):
    """
    删除指定文件的所有审核结果
    
    - **file_id**: 文件ID
    - **violation_result**: 可选，只删除指定类型的结果
    """
    # 检查文件是否存在
    file_obj = db.query(ReviewFile).filter(ReviewFile.id == file_id).first()
    if not file_obj:
        raise NotFoundError(f"文件不存在: {file_id}")
    
    # 构建删除查询
    query = db.query(ReviewResult).filter(ReviewResult.file_id == file_id)
    
    # 如果指定了结果类型，只删除该类型
    if violation_result:
        query = query.filter(ReviewResult.violation_result == violation_result)
    
    # 获取要删除的数量
    delete_count = query.count()
    
    if delete_count == 0:
        return APIResponse.success(
            data={"deleted_count": 0},
            message="没有找到要删除的审核结果"
        )
    
    # 执行删除
    query.delete(synchronize_session=False)
    db.commit()
    
    # 更新文件统计
    from app.services.file_service import FileService
    file_service = FileService(db)
    try:
        file_service.update_file_violation_count(file_id)
    except Exception as e:
        logger.warning(f"更新文件统计失败: {e}")
    
    return APIResponse.success(
        data={
            "deleted_count": delete_count,
            "file_id": file_id,
            "result_type": violation_result.value if violation_result else "全部"
        },
        message=f"成功删除文件的{delete_count}个审核结果"
    )


@router.delete("/task/{task_id}/all", summary="删除任务的所有审核结果")
async def delete_task_all_results(
    task_id: str = Path(..., description="任务ID"),
    violation_result: Optional[ViolationResult] = Query(None, description="只删除指定结果类型"),
    confirm: bool = Query(False, description="确认删除（必须为true）"),
    db: Session = Depends(get_db)
):
    """
    删除指定任务的所有审核结果
    
    - **task_id**: 任务ID
    - **violation_result**: 可选，只删除指定类型的结果
    - **confirm**: 必须设置为true才能执行删除
    """
    if not confirm:
        raise ValidationError("删除任务所有结果需要确认，请设置confirm=true")
    
    # 检查任务是否存在
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    task = task_service.get_task_by_id(task_id)
    
    # 构建删除查询
    query = db.query(ReviewResult).join(ReviewFile).filter(
        ReviewFile.task_id == task_id
    )
    
    # 如果指定了结果类型，只删除该类型
    if violation_result:
        query = query.filter(ReviewResult.violation_result == violation_result)
    
    # 获取受影响的文件ID
    affected_files = db.query(ReviewFile.id).filter(
        ReviewFile.task_id == task_id
    ).all()
    affected_file_ids = [str(file.id) for file in affected_files]
    
    # 获取要删除的数量
    delete_count = query.count()
    
    if delete_count == 0:
        return APIResponse.success(
            data={"deleted_count": 0},
            message="没有找到要删除的审核结果"
        )
    
    # 执行删除
    query.delete(synchronize_session=False)
    db.commit()
    
    # 更新所有受影响文件的统计
    from app.services.file_service import FileService
    file_service = FileService(db)
    for file_id in affected_file_ids:
        try:
            file_service.update_file_violation_count(file_id)
        except Exception as e:
            logger.warning(f"更新文件{file_id}统计失败: {e}")
    
    # 更新任务统计
    try:
        task_service.update_task_progress(task_id)
    except Exception as e:
        logger.warning(f"更新任务统计失败: {e}")
    
    return APIResponse.success(
        data={
            "deleted_count": delete_count,
            "task_id": task_id,
            "result_type": violation_result.value if violation_result else "全部",
            "affected_files": len(affected_file_ids)
        },
        message=f"成功删除任务的{delete_count}个审核结果"
    )