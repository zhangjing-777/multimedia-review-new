"""
审核结果API路由
提供审核结果查询、人工标注等接口
"""

from datetime import datetime
import re
import os
from typing import List, Optional, Dict
from fastapi import APIRouter, Depends, Query, Path, Body
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db
from app.models.result import ReviewResult, ViolationResult, SourceType
from app.models.file import ReviewFile
from app.utils.response import APIResponse, NotFoundError
from app.services.file_service import FileService
from app.services.task_service import TaskService


def _add_image_url(result_dict):
    """为结果添加图片URL"""
    position = result_dict.get("position", {})
    static_url = position.get("static_url")
    if static_url:
        result_dict["image_url"] = static_url  
        result_dict["has_image"] = True
    else:
        result_dict["has_image"] = False
    return result_dict

# 创建路由器
router = APIRouter()


# 请求模型定义
class MarkResultRequest(BaseModel):
    """标记审核结果请求模型（增强版）"""
    reviewer_id: str = Field(..., description="复审人员ID")
    review_result: str = Field(..., pattern="^(confirmed|rejected|modified)$", description="复审结果")
    review_comment: Optional[str] = Field(None, max_length=500, description="复审备注")
    
    # 新增：可修改的字段
    violation_result: Optional[ViolationResult] = Field(None, description="修正后的检测结果")
    confidence_score: Optional[float] = Field(None, ge=0, le=1, description="修正后的置信度")
    evidence: Optional[str] = Field(None, max_length=1000, description="修正后的证据描述")
    evidence_text: Optional[str] = Field(None, max_length=2000, description="修正后的证据文本")
    position: Optional[Dict] = Field(None, description="修正后的位置信息")
    page_number: Optional[int] = Field(None, ge=1, description="修正后的页码")
    timestamp: Optional[float] = Field(None, ge=0, description="修正后的时间戳")

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
    result_list = [_add_image_url(result.to_dict()) for result in results]
    
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
        result_dict = _add_image_url(result.to_dict())
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
    result_list = [_add_image_url(result.to_dict()) for result in results]
    
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
        data=_add_image_url(result.to_dict()),
        message="查询成功"
    )


@router.post("/{result_id}/mark", summary="人工标记并修正审核结果")
async def mark_result(
    request: MarkResultRequest,
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    人工标记/修改审核结果（增强版）
    
    支持在标记的同时修正AI识别的错误：
    - **reviewer_id**: 复审人员ID
    - **review_result**: 复审结果 (confirmed/rejected/modified)
    - **review_comment**: 复审备注（可选）
    
    可修正的字段：
    - **violation_result**: 修正检测结果（合规/不合规/不确定）
    - **confidence_score**: 修正置信度分数
    - **evidence**: 修正证据描述
    - **evidence_text**: 修正证据文本
    - **position**: 修正位置信息
    - **page_number**: 修正页码
    - **timestamp**: 修正时间戳
    
    当 review_result 为 "modified" 时，建议同时提供要修正的字段。
    """
    
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    # 记录修改前的值（用于日志和审计）
    original_values = {}
    modified_fields = []
    
    # 检查并应用字段修改
    if request.violation_result is not None and request.violation_result != result.violation_result:
        original_values["violation_result"] = result.violation_result.value if result.violation_result else None
        result.violation_result = request.violation_result
        modified_fields.append("violation_result")
    
    if request.confidence_score is not None and request.confidence_score != result.confidence_score:
        original_values["confidence_score"] = result.confidence_score
        result.confidence_score = request.confidence_score
        modified_fields.append("confidence_score")
    
    if request.evidence is not None and request.evidence != result.evidence:
        original_values["evidence"] = result.evidence
        result.evidence = request.evidence
        modified_fields.append("evidence")
    
    if request.evidence_text is not None and request.evidence_text != result.evidence_text:
        original_values["evidence_text"] = result.evidence_text
        result.evidence_text = request.evidence_text
        modified_fields.append("evidence_text")
    
    if request.position is not None and request.position != result.position:
        original_values["position"] = result.position
        result.position = request.position
        modified_fields.append("position")
    
    if request.page_number is not None and request.page_number != result.page_number:
        original_values["page_number"] = result.page_number
        result.page_number = request.page_number
        modified_fields.append("page_number")
    
    if request.timestamp is not None and request.timestamp != result.timestamp:
        original_values["timestamp"] = result.timestamp
        result.timestamp = request.timestamp
        modified_fields.append("timestamp")
    
    # 构建完整的复审备注
    full_comment = request.review_comment or ""
    if modified_fields:
        modification_log = f"修改字段: {', '.join(modified_fields)}"
        if original_values:
            modification_log += f" | 原值: {original_values}"
        full_comment = f"{full_comment}\n[系统记录] {modification_log}" if full_comment else f"[系统记录] {modification_log}"
    
    # 标记为已复审
    result.mark_reviewed(
        reviewer_id=request.reviewer_id,
        result=request.review_result,
        comment=full_comment
    )
    
    # 更新时间戳
    result.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(result)
    
    # 记录操作日志
    logger.info(f"用户 {request.reviewer_id} 标记结果 {result_id}: {request.review_result}" + 
               (f", 修改字段: {modified_fields}" if modified_fields else ""))
    
    # 返回结果，包含修改信息
    response_data = _add_image_url(result.to_dict())
    response_data["modification_info"] = {
        "modified_fields": modified_fields,
        "original_values": original_values,
        "total_modifications": len(modified_fields)
    }
    
    return APIResponse.success(
        data=response_data,
        message=f"审核结果标记成功" + (f"，修改了 {len(modified_fields)} 个字段" if modified_fields else "")
    )


@router.post("/batch/mark", summary="批量标记审核结果")
async def batch_mark_results(
    result_ids: List[str] = Body(..., description="结果ID列表"),
    reviewer_id: str = Body(..., description="复审人员ID"),
    review_result: str = Body(..., pattern="^(confirmed|rejected|modified)$", description="复审结果"),
    review_comment: Optional[str] = Body(None, description="复审备注"),
    # 批量修改时只支持部分字段，避免复杂性
    violation_result: Optional[ViolationResult] = Body(None, description="统一修正的检测结果"),
    confidence_score: Optional[float] = Body(None, ge=0, le=1, description="统一修正的置信度"),
    db: Session = Depends(get_db)
):
    """
    批量标记审核结果
    
    支持对多个结果进行统一标记和修正：
    - 最多支持50个结果的批量操作
    - 支持统一修改检测结果和置信度
    - 自动记录批量操作日志
    """
    if len(result_ids) > 50:
        raise ValidationError("批量标记最多支持50个结果")
    
    if not result_ids:
        raise ValidationError("结果ID列表不能为空")
    
    # 查找存在的结果
    results = db.query(ReviewResult).filter(
        ReviewResult.id.in_(result_ids)
    ).all()
    
    if not results:
        raise NotFoundError("没有找到要标记的审核结果")
    
    marked_ids = []
    not_found_ids = []
    modified_count = 0
    
    # 检查哪些ID存在
    found_ids = {str(result.id) for result in results}
    for result_id in result_ids:
        if result_id not in found_ids:
            not_found_ids.append(result_id)
    
    # 批量处理
    for result in results:
        try:
            modified_fields = []
            
            # 应用统一修改
            if violation_result is not None and violation_result != result.violation_result:
                result.violation_result = violation_result
                modified_fields.append("violation_result")
            
            if confidence_score is not None and confidence_score != result.confidence_score:
                result.confidence_score = confidence_score
                modified_fields.append("confidence_score")
            
            # 构建批量操作的备注
            batch_comment = review_comment or ""
            if modified_fields:
                batch_modification = f"批量修改: {', '.join(modified_fields)}"
                batch_comment = f"{batch_comment}\n[批量操作] {batch_modification}" if batch_comment else f"[批量操作] {batch_modification}"
            
            # 标记复审
            result.mark_reviewed(
                reviewer_id=reviewer_id,
                result=review_result,
                comment=batch_comment
            )
            
            result.updated_at = datetime.utcnow()
            marked_ids.append(str(result.id))
            
            if modified_fields:
                modified_count += 1
                
        except Exception as e:
            logger.error(f"批量标记结果 {result.id} 失败: {e}")
    
    db.commit()
    
    # 记录批量操作日志
    logger.info(f"用户 {reviewer_id} 批量标记 {len(marked_ids)} 个结果: {review_result}" + 
               (f", 修改了 {modified_count} 个结果" if modified_count > 0 else ""))
    
    return APIResponse.success(
        data={
            "marked_count": len(marked_ids),
            "marked_ids": marked_ids,
            "not_found_ids": not_found_ids,
            "modified_count": modified_count,
            "total_requested": len(result_ids)
        },
        message=f"批量标记完成，成功标记 {len(marked_ids)} 个结果" + 
                (f"，修改了 {modified_count} 个结果" if modified_count > 0 else "")
    )


@router.get("/{result_id}/frame-info", summary="获取审核结果的完整帧信息")
async def get_result_frame_info(
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    获取审核结果对应的完整视频帧信息
    
    返回违规结果与视频帧的精确对应关系
    """
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    # 检查是否有帧信息
    if not result.position:
        raise ValidationError("该审核结果不包含帧信息")
    
    frame_info = result.position
    
    # 验证帧文件是否存在
    frame_exists = False
    if "frame_path" in frame_info:
        frame_exists = os.path.exists(frame_info["frame_path"])
    
    response_data = {
        "result_id": result_id,
        "violation_result": result.violation_result.value,
        "confidence_score": result.confidence_score,
        "evidence": result.evidence,
        
        # 完整的帧对应信息
        "frame_info": {
            "frame_number": result.page_number,
            "timestamp": result.timestamp,
            "frame_metadata": frame_info,
            "frame_exists": frame_exists,
            
            # API访问路径
            "frame_download_url": f"/api/v1/results/{result_id}/frame",
            "frame_view_url": f"/api/v1/upload/video/{result.file_id}/frame/{frame_info.get('filename', '')}"
        },
        
        # 文件信息
        "file_info": {
            "file_id": str(result.file_id),
            "original_video": frame_info.get("original_video", ""),
        }
    }
    
    return APIResponse.success(
        data=response_data,
        message="帧信息获取成功"
    )


@router.get("/file/{file_id}/frame-mapping", summary="获取文件的帧-结果映射关系")
async def get_file_frame_mapping(
    file_id: str = Path(..., description="文件ID"),
    db: Session = Depends(get_db)
):
    """
    获取视频文件的所有帧与审核结果的对应关系
    
    返回每一帧对应的所有审核结果
    """
    # 获取文件的所有审核结果
    results = db.query(ReviewResult).filter(
        ReviewResult.file_id == file_id
    ).order_by(ReviewResult.timestamp.asc()).all()
    
    if not results:
        return APIResponse.success(
            data={"file_id": file_id, "frame_mappings": []},
            message="该文件没有审核结果"
        )
    
    # 按帧组织结果
    frame_mappings = {}
    
    for result in results:
        frame_number = result.page_number or 0
        timestamp = result.timestamp or 0
        
        frame_key = f"frame_{frame_number}"
        
        if frame_key not in frame_mappings:
            frame_info = result.position or {}
            frame_mappings[frame_key] = {
                "frame_number": frame_number,
                "timestamp": timestamp,
                "frame_metadata": frame_info,
                "violations": []
            }
        
        # 添加违规结果
        violation_info = {
            "result_id": str(result.id),
            "violation_result": result.violation_result.value,
            "confidence_score": result.confidence_score,
            "evidence": result.evidence,
            "source_type": result.source_type.value,
            "is_reviewed": result.is_reviewed
        }
        
        frame_mappings[frame_key]["violations"].append(violation_info)
    
    # 转换为列表并排序
    sorted_mappings = sorted(frame_mappings.values(), key=lambda x: x["timestamp"])
    
    return APIResponse.success(
        data={
            "file_id": file_id,
            "total_frames": len(sorted_mappings),
            "total_violations": len(results),
            "frame_mappings": sorted_mappings
        },
        message="帧映射关系获取成功"
    )


@router.get("/{result_id}/history", summary="获取结果修改历史")
async def get_result_history(
    result_id: str = Path(..., description="结果ID"),
    db: Session = Depends(get_db)
):
    """
    获取审核结果的修改历史记录
    
    从复审备注中解析修改历史，用于审计和追踪
    """
    result = db.query(ReviewResult).filter(
        ReviewResult.id == result_id
    ).first()
    
    if not result:
        raise NotFoundError(f"审核结果不存在: {result_id}")
    
    # 解析修改历史
    history = []
    if result.review_comment:
        
        # 解析系统记录的修改信息
        system_records = re.findall(r'\[系统记录\] (.+?)(?=\n|\[|$)', result.review_comment)
        batch_records = re.findall(r'\[批量操作\] (.+?)(?=\n|\[|$)', result.review_comment)
        
        for record in system_records:
            history.append({
                "type": "individual_modification",
                "description": record,
                "timestamp": result.review_time.isoformat() if result.review_time else None
            })
        
        for record in batch_records:
            history.append({
                "type": "batch_modification", 
                "description": record,
                "timestamp": result.review_time.isoformat() if result.review_time else None
            })
    
    return APIResponse.success(
        data={
            "result_id": result_id,
            "current_values": _add_image_url(result.to_dict()),
            "modification_history": history,
            "is_reviewed": result.is_reviewed,
            "reviewer_id": result.reviewer_id,
            "review_result": result.review_result,
            "last_modified": result.updated_at.isoformat() if result.updated_at else None
        },
        message="修改历史获取成功"
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