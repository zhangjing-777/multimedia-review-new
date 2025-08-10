"""
审核任务处理器
负责异步处理审核任务和文件
"""

import asyncio
from typing import List, Dict
from celery import current_task
from sqlalchemy.orm import Session
from loguru import logger
import PyPDF2
from pdf2image import convert_from_path
from docx import Document
import cv2
import uuid
import os

from app.workers.celery_app import celery_app
from app.database import SessionLocal
from app.services import (
    TaskService, FileService, OCRService, 
    AIReviewService, QueueService
)
from app.models.task import TaskStatus, ReviewTask
from app.models.file import FileStatus, FileType, ReviewFile
from app.models.result import ReviewResult, ViolationResult, SourceType
from app.utils.file_utils import FileUtils


@celery_app.task(bind=True, name="process_review_task")
def process_review_task(self, task_id: str):
    """
    处理审核任务的主流程（带锁机制）
    """
    db = SessionLocal()
    queue_service = QueueService()
    
    try:
        # 使用分布式锁确保任务唯一性
        with queue_service.task_lock(task_id):
            task_service = TaskService(db)
            file_service = FileService(db)
            
            # 获取任务信息
            task = task_service.get_task_by_id(task_id)
            logger.info(f"🔒 开始处理任务: {task.name} (已加锁)")
            
            # 检查任务状态，避免重复处理
            if task.status not in [TaskStatus.PENDING, TaskStatus.PROCESSING]:
                logger.warning(f"任务 {task_id} 状态为 {task.status}，跳过处理")
                return {"status": "skipped", "reason": f"任务状态: {task.status}"}
            
            # 获取任务的所有文件
            files = task_service.get_task_files(task_id, status=FileStatus.PENDING)
            
            if not files:
                task_service.complete_task(task_id, success=False, error_message="没有待处理的文件")
                return {"status": "failed", "message": "没有待处理的文件"}
            
            # 将所有文件添加到处理队列
            for file_obj in files:
                queue_service.add_file_to_queue(
                    file_id=str(file_obj.id),
                    task_id=task_id,
                    file_type=file_obj.file_type.value
                )
            
            # 更新任务进度
            queue_service.update_progress(
                task_id, 
                progress=10, 
                message=f"已将{len(files)}个文件加入处理队列"
            )
            
            return {
                "status": "processing",
                "message": f"任务已启动，{len(files)}个文件进入处理队列"
            }
    
    except RuntimeError as e:
        # 锁冲突，任务已被其他进程处理
        logger.warning(f"任务锁冲突: {e}")
        return {"status": "skipped", "reason": "任务正在被其他进程处理"}
    
    except Exception as e:
        logger.error(f"处理任务失败 {task_id}: {e}")
        
        try:
            task_service = TaskService(db)
            task_service.complete_task(task_id, success=False, error_message=str(e))
        except Exception as save_error:
            logger.error(f"保存任务失败状态时出错: {save_error}")
        
        return {"status": "failed", "error": str(e)}
    
    finally:
        db.close()


@celery_app.task(bind=True, name="process_review_file") 
def process_review_file(self, file_id: str, task_id: str, file_type: str):
    """
    处理单个文件的审核（带锁机制）
    """
    db = SessionLocal()
    queue_service = QueueService()
    
    try:
        # 使用分布式锁确保文件唯一性
        with queue_service.file_lock(file_id):
            file_service = FileService(db)
            
            # 获取文件信息
            file_obj = file_service.get_file_by_id(file_id)
            logger.info(f"🔒 开始处理文件: {file_obj.original_name} (已加锁)")
            
            # 检查文件状态，避免重复处理
            if file_obj.status not in [FileStatus.PENDING, FileStatus.PROCESSING]:
                logger.warning(f"文件 {file_id} 状态为 {file_obj.status}，跳过处理")
                return {"status": "skipped", "reason": f"文件状态: {file_obj.status}"}
            
            # 更新文件状态为处理中
            file_service.update_file_status(file_id, FileStatus.PROCESSING, progress=0)
            
            # 根据文件类型进行不同处理
            if file_obj.file_type == FileType.DOCUMENT:
                result = _process_document_file(file_obj, db)
            elif file_obj.file_type == FileType.IMAGE:
                result = _process_image_file(file_obj, db)
            elif file_obj.file_type == FileType.VIDEO:
                result = _process_video_file(file_obj, db)
            elif file_obj.file_type == FileType.TEXT:
                result = _process_text_file(file_obj, db)
            else:
                raise ValueError(f"不支持的文件类型: {file_obj.file_type}")
            
            # 更新文件处理完成状态
            file_service.update_file_status(file_id, FileStatus.COMPLETED, progress=100)
            
            # 更新文件统计信息
            file_service.update_file_violation_count(file_id)
            
            # 更新任务进度
            _update_task_progress(task_id, db)
            
            logger.info(f"✅ 文件处理完成: {file_obj.original_name}, 发现{len(result)}个检测结果")
            
            return {
                "status": "completed",
                "file_id": file_id,
                "results_count": len(result),
                "message": "文件处理完成"
            }
    
    except RuntimeError as e:
        # 锁冲突，文件已被其他进程处理
        logger.warning(f"文件锁冲突: {e}")
        return {"status": "skipped", "reason": "文件正在被其他进程处理"}
    
    except Exception as e:
        logger.error(f"处理文件失败 {file_id}: {e}")
        
        try:
            file_service = FileService(db)
            file_service.update_file_status(
                file_id, 
                FileStatus.FAILED, 
                error_message=str(e)
            )
            _update_task_progress(task_id, db)
        except Exception as update_error:
            logger.error(f"更新文件失败状态时出错: {update_error}")
        
        return {"status": "failed", "file_id": file_id, "error": str(e)}
    
    finally:
        db.close()


def _process_document_file(file_obj, db: Session) -> List[Dict]:
    """处理文档文件（修复版）"""
    try:
        # 获取任务信息
        task = db.query(ReviewTask).filter(ReviewTask.id == file_obj.task_id).first()
        if not task:
            logger.error(f"无法找到任务: {file_obj.task_id}")
            return []
        
        strategy_type = task.strategy_type
        strategy_contents = task.strategy_contents
        
        logger.info(f"开始处理文档: {file_obj.original_name}")
        
        # 检查文件是否存在
        if not os.path.exists(file_obj.file_path):
            logger.error(f"文件不存在: {file_obj.file_path}")
            return []
        
        # 根据文件类型选择处理方式
        file_ext = os.path.splitext(file_obj.original_name)[1].lower()
        
        if file_ext == '.pdf':
            return _process_pdf_file(file_obj, strategy_type, strategy_contents, db)
        elif file_ext in ['.docx', '.doc']:
            return _process_word_file(file_obj, strategy_type, strategy_contents, db)
        elif file_ext == '.txt':
            return _process_text_file(file_obj, db)
        else:
            logger.warning(f"不支持的文档类型: {file_ext}")
            return []
    
    except Exception as e:
        logger.error(f"文档处理失败: {e}", exc_info=True)
        return []

def _process_pdf_file(file_obj, strategy_type: str, strategy_contents: str, db: Session) -> List[Dict]:
    """处理PDF文件"""
    try:
        
        logger.info(f"处理PDF文件: {file_obj.original_name}")
        
        all_violations = []
        
        # 方法1：提取文本内容
        try:
            with open(file_obj.file_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                text_content = ""
                
                for page_num, page in enumerate(pdf_reader.pages, 1):
                    page_text = page.extract_text()
                    if page_text.strip():
                        text_content += f"\n[页面 {page_num}]\n{page_text}"
                
                if text_content.strip():
                    logger.info(f"从PDF提取到文本，长度: {len(text_content)}")
                    text_violations = _review_text_content_sync(
                        text_content, strategy_type, strategy_contents
                    )
                    all_violations.extend(text_violations)
        
        except Exception as e:
            logger.warning(f"PDF文本提取失败: {e}")
        
        # 方法2：转换为图片进行OCR
        try:
            logger.info("将PDF转换为图片进行OCR处理")
            images = convert_from_path(file_obj.file_path, dpi=200, first_page=1, last_page=5)  # 限制前5页
            
            for page_num, image in enumerate(images, 1):
                # 保存临时图片
                temp_image_path = f"/tmp/pdf_page_{file_obj.id}_{page_num}.jpg"
                image.save(temp_image_path, 'JPEG')
                
                try:
                    # OCR + 视觉审核
                    page_violations = _process_image_content_sync(
                        temp_image_path, strategy_type, strategy_contents, page_num
                    )
                    all_violations.extend(page_violations)
                    
                finally:
                    # 清理临时文件
                    if os.path.exists(temp_image_path):
                        os.remove(temp_image_path)
        
        except Exception as e:
            logger.warning(f"PDF图片转换失败: {e}")
        
        # 更新OCR统计
        _update_file_ocr_stats(file_obj, len(all_violations), db)
        
        logger.info(f"PDF处理完成，发现 {len(all_violations)} 个检测结果")
        return all_violations
    
    except Exception as e:
        logger.error(f"PDF处理失败: {e}", exc_info=True)
        return []

def _process_word_file(file_obj, strategy_type: str, strategy_contents: str, db: Session) -> List[Dict]:
    """处理Word文件"""
    try:
        
        logger.info(f"处理Word文件: {file_obj.original_name}")
        
        # 提取Word文档文本
        doc = Document(file_obj.file_path)
        text_content = ""
        
        for para in doc.paragraphs:
            if para.text.strip():
                text_content += para.text + "\n"
        
        if not text_content.strip():
            logger.warning("Word文档中没有提取到文本内容")
            return []
        
        logger.info(f"从Word提取到文本，长度: {len(text_content)}")
        
        # 审核文本内容
        violations = _review_text_content_sync(text_content, strategy_type, strategy_contents)
        
        # 更新统计
        _update_file_ocr_stats(file_obj, len(violations), db)
        
        logger.info(f"Word处理完成，发现 {len(violations)} 个检测结果")
        return violations
    
    except Exception as e:
        logger.error(f"Word文件处理失败: {e}", exc_info=True)
        return []

def _process_image_content_sync(image_path: str, strategy_type: str, strategy_contents: str, page_num: int = 1) -> List[Dict]:
    """同步处理图片内容"""
    try:
        
        ocr_service = OCRService()
        ai_service = AIReviewService()
        
        # 使用事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            violations = []
            
            # OCR提取内容
            ocr_result = loop.run_until_complete(
                ocr_service.extract_content(image_path)
            )
            
            if ocr_result.get("success"):
                # 处理文本块
                text_blocks = [block for block in ocr_result.get("blocks", []) 
                              if block["type"] == "text"]
                if text_blocks:
                    text_content = " ".join([block["text"] for block in text_blocks])
                    if text_content.strip():
                        text_violations = loop.run_until_complete(
                            ai_service.review_text_content(text_content, strategy_type, strategy_contents)
                        )
                        
                        for violation in text_violations:
                            violation["page_number"] = page_num
                            violations.append(violation)
            
            # 视觉内容审核
            visual_violations = loop.run_until_complete(
                ai_service.review_visual_content(image_path, strategy_type, strategy_contents)
            )
            
            for violation in visual_violations:
                violation["page_number"] = page_num
                violations.append(violation)
            
            return violations
        
        finally:
            loop.close()
    
    except Exception as e:
        logger.error(f"图片内容处理失败: {e}", exc_info=True)
        return []

def _review_text_content_sync(text_content: str, strategy_type: str, strategy_contents: str) -> List[Dict]:
    """同步审核文本内容"""
    try:
        
        ai_service = AIReviewService()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            violations = loop.run_until_complete(
                ai_service.review_text_content(text_content, strategy_type, strategy_contents)
            )
            return violations
        finally:
            loop.close()
    
    except Exception as e:
        logger.error(f"文本审核失败: {e}", exc_info=True)
        return []

def _update_file_ocr_stats(file_obj, results_count: int, db: Session):
    """更新文件OCR统计"""
    try:
        
        file_service = FileService(db)
        file_service.update_file_ocr_stats(
            str(file_obj.id), 
            results_count,  # total blocks
            results_count,  # text blocks (简化)
            0               # image blocks
        )
    except Exception as e:
        logger.warning(f"更新文件统计失败: {e}")

def _update_task_progress(task_id: str, db: Session):
    """
    更新任务进度（带锁保护）
    """
    try:
        
        task_service = TaskService(db)
        
        # 统计已完成的文件数
        completed_files = db.query(ReviewFile).filter(
            ReviewFile.task_id == task_id,
            ReviewFile.status.in_([FileStatus.COMPLETED, FileStatus.FAILED])
        ).count()
        
        # 更新任务进度
        task_service.update_task_progress(task_id, completed_files)
        
        # 检查是否所有文件都处理完成
        total_files = db.query(ReviewFile).filter(
            ReviewFile.task_id == task_id
        ).count()
        
        if completed_files >= total_files:
            # 获取失败文件数
            failed_files = db.query(ReviewFile).filter(
                ReviewFile.task_id == task_id,
                ReviewFile.status == FileStatus.FAILED
            ).count()
            
            # 根据失败文件数决定任务状态
            if failed_files == 0:
                task_service.complete_task(task_id, success=True)
                logger.info(f"✅ 任务 {task_id} 全部完成")
            elif failed_files < total_files:
                task_service.complete_task(task_id, success=True)
                logger.info(f"⚠️ 任务 {task_id} 部分完成，{failed_files}/{total_files} 文件失败")
            else:
                task_service.complete_task(task_id, success=False, error_message="所有文件处理失败")
                logger.error(f"❌ 任务 {task_id} 全部失败")
    
    except Exception as e:
        logger.error(f"更新任务进度失败: {e}")


def _process_image_file(file_obj, db: Session) -> List[Dict]:
    """处理图片文件"""
    ocr_service = OCRService()
    ai_service = AIReviewService()
    
    # 修复：正确获取任务信息
    task = db.query(ReviewTask).filter(ReviewTask.id == file_obj.task_id).first()
    if not task:
        logger.error(f"无法找到任务: {file_obj.task_id}")
        return []
    
    strategy_type = task.strategy_type
    strategy_contents = task.strategy_contents
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        all_violations = []
        
        # OCR提取内容
        ocr_result = loop.run_until_complete(
            ocr_service.extract_content(file_obj.file_path)
        )
        
        if ocr_result.get("success"):
            # 处理文本块
            text_blocks = [block for block in ocr_result.get("blocks", []) 
                          if block["type"] == "text"]
            if text_blocks:
                text_content = " ".join([block["text"] for block in text_blocks])
                text_violations = loop.run_until_complete(
                    ai_service.review_text_content(text_content, strategy_type, strategy_contents)
                )
                
                for violation in text_violations:
                    _save_violation_result(violation, str(file_obj.id), 1, db)
                    all_violations.append(violation)
        
        # 直接对整个图片进行视觉审核
        visual_violations = loop.run_until_complete(
            ai_service.review_visual_content(file_obj.file_path, strategy_type, strategy_contents)
        )
        
        for violation in visual_violations:
            _save_violation_result(violation, str(file_obj.id), 1, db)
            all_violations.append(violation)
        
        return all_violations
    
    finally:
        loop.close()


def _process_video_file(file_obj, db: Session) -> List[Dict]:
    """处理视频文件（修复版）"""
    try:
        # 获取任务信息
        task = db.query(ReviewTask).filter(ReviewTask.id == file_obj.task_id).first()
        if not task:
            logger.error(f"无法找到任务: {file_obj.task_id}")
            return []
        
        strategy_type = task.strategy_type
        strategy_contents = task.strategy_contents
        frame_interval = task.video_frame_interval or 5
        
        logger.info(f"开始处理视频: {file_obj.original_name}, 抽帧间隔: {frame_interval}秒")
        
        # 检查文件是否存在
        if not os.path.exists(file_obj.file_path):
            logger.error(f"视频文件不存在: {file_obj.file_path}")
            return []
        
        # 提取视频帧
        frame_paths = _extract_video_frames_fixed(file_obj.file_path, frame_interval)
        
        if not frame_paths:
            logger.warning("没有成功提取到视频帧")
            return []
        
        logger.info(f"成功提取 {len(frame_paths)} 个视频帧")
        
        all_violations = []
        
        try:
            # 处理每一帧
            for i, frame_path in enumerate(frame_paths):
                frame_time = i * frame_interval
                
                logger.info(f"处理第 {i+1}/{len(frame_paths)} 帧, 时间: {frame_time}s")
                
                # 处理单帧
                frame_violations = _process_image_content_sync(
                    frame_path, strategy_type, strategy_contents
                )
                
                # 添加时间戳信息
                for violation in frame_violations:
                    violation["timestamp"] = frame_time
                    violation["position"] = {"timestamp": frame_time, "frame_number": i+1}
                    all_violations.append(violation)
        
        finally:
            # 清理临时帧文件
            _cleanup_temp_files(frame_paths)
        
        # 更新统计
        _update_file_ocr_stats(file_obj, len(all_violations), db)
        
        logger.info(f"视频处理完成，发现 {len(all_violations)} 个检测结果")
        return all_violations
    
    except Exception as e:
        logger.error(f"视频处理失败: {e}", exc_info=True)
        return []

def _extract_video_frames_fixed(video_path: str, interval: int = 5, max_frames: int = 20) -> List[str]:
    """提取视频帧（修复版）"""
    try:
        
        frame_paths = []
        
        # 打开视频
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"无法打开视频文件: {video_path}")
            return []
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            logger.error("无法获取视频帧率")
            cap.release()
            return []
        
        frame_interval = int(fps * interval)  # 间隔帧数
        
        # 创建临时目录
        temp_dir = f"/tmp/video_frames_{uuid.uuid4()}"
        os.makedirs(temp_dir, exist_ok=True)
        
        frame_num = 0
        saved_frames = 0
        
        logger.info(f"开始提取视频帧，FPS: {fps}, 间隔: {frame_interval}帧")
        
        while saved_frames < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            
            # 按间隔保存帧
            if frame_num % frame_interval == 0:
                frame_path = os.path.join(temp_dir, f"frame_{saved_frames:04d}.jpg")
                if cv2.imwrite(frame_path, frame):
                    frame_paths.append(frame_path)
                    saved_frames += 1
                    logger.debug(f"保存帧 {saved_frames}: {frame_path}")
                else:
                    logger.warning(f"保存帧失败: {frame_path}")
            
            frame_num += 1
        
        cap.release()
        
        logger.info(f"视频帧提取完成，共提取 {len(frame_paths)} 帧")
        return frame_paths
    
    except Exception as e:
        logger.error(f"视频帧提取失败: {e}", exc_info=True)
        return []

def _cleanup_temp_files(file_paths: List[str]):
    """清理临时文件"""
    try:
        for file_path in file_paths:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"清理临时文件: {file_path}")
            except Exception as e:
                logger.warning(f"清理临时文件失败 {file_path}: {e}")
        
        # 清理临时目录
        if file_paths:
            temp_dir = os.path.dirname(file_paths[0])
            try:
                if os.path.exists(temp_dir) and os.path.isdir(temp_dir):
                    os.rmdir(temp_dir)
                    logger.debug(f"清理临时目录: {temp_dir}")
            except Exception as e:
                logger.warning(f"清理临时目录失败 {temp_dir}: {e}")
    
    except Exception as e:
        logger.warning(f"清理临时文件过程出错: {e}")

# 修复文本文件处理
def _process_text_file(file_obj, db: Session) -> List[Dict]:
    """处理纯文本文件（修复版）"""
    try:
        # 获取任务信息
        task = db.query(ReviewTask).filter(ReviewTask.id == file_obj.task_id).first()
        if not task:
            logger.error(f"无法找到任务: {file_obj.task_id}")
            return []
        
        strategy_type = task.strategy_type
        strategy_contents = task.strategy_contents
        
        logger.info(f"开始处理文本文件: {file_obj.original_name}")
        
        # 检查文件是否存在
        if not os.path.exists(file_obj.file_path):
            logger.error(f"文本文件不存在: {file_obj.file_path}")
            return []
        
        # 读取文本内容
        text_content = ""
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin1']
        
        for encoding in encodings:
            try:
                with open(file_obj.file_path, 'r', encoding=encoding) as f:
                    text_content = f.read()
                logger.info(f"成功使用 {encoding} 编码读取文件")
                break
            except UnicodeDecodeError:
                continue
        
        if not text_content:
            logger.error("无法读取文本文件内容")
            return []
        
        if not text_content.strip():
            logger.warning("文本文件内容为空")
            return []
        
        logger.info(f"读取到文本内容，长度: {len(text_content)}")
        
        # 审核文本内容
        violations = _review_text_content_sync(text_content, strategy_type, strategy_contents)
        
        # 更新统计
        _update_file_ocr_stats(file_obj, len(violations), db)
        
        logger.info(f"文本文件处理完成，发现 {len(violations)} 个检测结果")
        return violations
    
    except Exception as e:
        logger.error(f"文本文件处理失败: {e}", exc_info=True)
        return []


def _save_violation_result(
    violation: Dict, 
    file_id: str, 
    page_number: int = None,
    db: Session = None,
    timestamp: float = None
):
    """保存检测结果到数据库（包括合规、不合规、不确定三种结果）"""
    try:
        
        # 获取检测结果
        violation_result_str = violation.get("violation_result", "不确定")
        violation_result = _get_violation_result_enum(violation_result_str)
        
        # 确保数据库会话可用
        if db is None:
            logger.error("数据库会话为空，无法保存检测结果")
            return
        
        # 创建审核结果对象
        result = ReviewResult(
            file_id=file_id,
            violation_result=violation_result, 
            source_type=SourceType(violation.get("source_type", "ocr")),
            confidence_score=float(violation.get("confidence_score", 0.0)),
            evidence=violation.get("evidence", ""),
            evidence_text=violation.get("evidence_text"),
            position=violation.get("position"),
            page_number=page_number,
            timestamp=timestamp,
            model_name=violation.get("model_name"),
            model_version=violation.get("model_version"),
            raw_response=violation.get("raw_response")
        )
        
        # 保存到数据库
        db.add(result)
        db.commit()
        logger.info(f"成功保存检测结果: {violation_result.value}, 置信度: {result.confidence_score}")
    
    except Exception as e:
        logger.error(f"保存检测结果失败: {e}")
        if db:
            try:
                db.rollback()
            except Exception as rollback_error:
                logger.error(f"回滚事务失败: {rollback_error}")

def _get_violation_result_enum(result_str: str) -> 'ViolationResult':
    """将字符串转换为检测结果枚举"""
    
    if not result_str:
        return ViolationResult.UNCERTAIN
        
    result_map = {
        "不合规": ViolationResult.NON_COMPLIANT,
        "合规": ViolationResult.COMPLIANT,
        "不确定": ViolationResult.UNCERTAIN,
        # 英文映射
        "non_compliant": ViolationResult.NON_COMPLIANT,
        "compliant": ViolationResult.COMPLIANT,
        "uncertain": ViolationResult.UNCERTAIN,
    }
    
    # 转换为小写进行匹配
    result_lower = result_str.lower()
    
    # 先尝试直接匹配
    if result_str in result_map:
        return result_map[result_str]
    
    # 再尝试小写匹配
    if result_lower in result_map:
        return result_map[result_lower]
    
    # 模糊匹配
    if "合规" in result_str or "compliant" in result_lower:
        return ViolationResult.COMPLIANT
    elif "不合规" in result_str or "违规" in result_str or "non" in result_lower:
        return ViolationResult.NON_COMPLIANT
    else:
        return ViolationResult.UNCERTAIN


@celery_app.task(name="cleanup_temp_files")
def cleanup_temp_files(file_paths: List[str]):
    """清理临时文件"""
    FileUtils.cleanup_temp_files(file_paths)
    return {"cleaned": len(file_paths)}
