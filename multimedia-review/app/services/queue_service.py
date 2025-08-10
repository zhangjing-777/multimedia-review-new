"""
重新设计的消息队列服务
添加分布式锁机制防止重复处理
"""

import redis
import json
from typing import Dict, Any, Optional
from datetime import datetime
from loguru import logger
from contextlib import contextmanager
from app.workers.review_worker import process_review_task, process_review_file
from celery import current_app

from app.config import get_settings


class QueueService:
    """带锁机制的消息队列服务类"""
    
    def __init__(self):
        self.settings = get_settings()
        
        # Redis 连接
        self.redis = redis.Redis.from_url(
            self.settings.CELERY_BROKER_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5
        )
        
        # 缓存 Redis 连接
        self.cache_redis = redis.Redis.from_url(
            self.settings.REDIS_CACHE_URL,
            decode_responses=True
        )
        
        logger.info(f"QueueService 初始化完成，使用 Celery Broker: {self.settings.CELERY_BROKER_URL}")
    
    @contextmanager
    def task_lock(self, task_id: str, timeout: int = 3600):
        """
        任务分布式锁上下文管理器
        
        Args:
            task_id: 任务ID
            timeout: 锁超时时间（秒）
        """
        lock_key = f"task_lock:{task_id}"
        lock_value = f"worker_{datetime.utcnow().timestamp()}"
        
        # 尝试获取锁
        acquired = self.cache_redis.set(lock_key, lock_value, nx=True, ex=timeout)
        
        if not acquired:
            raise RuntimeError(f"任务 {task_id} 正在被其他进程处理")
        
        try:
            logger.info(f"🔒 获取任务锁: {task_id}")
            yield
        finally:
            # 释放锁（只有锁的持有者才能释放）
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            self.cache_redis.eval(lua_script, 1, lock_key, lock_value)
            logger.info(f"🔓 释放任务锁: {task_id}")
    
    @contextmanager 
    def file_lock(self, file_id: str, timeout: int = 1800):
        """
        文件分布式锁上下文管理器
        
        Args:
            file_id: 文件ID
            timeout: 锁超时时间（秒）
        """
        lock_key = f"file_lock:{file_id}"
        lock_value = f"worker_{datetime.utcnow().timestamp()}"
        
        acquired = self.cache_redis.set(lock_key, lock_value, nx=True, ex=timeout)
        
        if not acquired:
            raise RuntimeError(f"文件 {file_id} 正在被其他进程处理")
        
        try:
            logger.info(f"🔒 获取文件锁: {file_id}")
            yield
        finally:
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            self.cache_redis.eval(lua_script, 1, lock_key, lock_value)
            logger.info(f"🔓 释放文件锁: {file_id}")
    
    def add_task_to_queue(self, task_id: str, priority: int = 0) -> bool:
        """
        将任务添加到 Celery 队列（检查重复）
        """
        try:
            # 检查任务是否已经在处理中
            if self.is_task_processing(task_id):
                logger.warning(f"任务 {task_id} 已在处理中，跳过重复提交")
                return False
            
            logger.info(f"🚀 提交任务到 Celery: {task_id}")
            result = process_review_task.delay(task_id)
            
            # 标记任务为处理中
            self.set_task_status(task_id, "submitted", {
                "celery_task_id": result.id,
                "priority": priority,
                "processing": True
            })
            
            logger.info(f"✅ 任务已提交，Celery任务ID: {result.id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ 提交任务失败: {e}")
            return False
    
    def add_file_to_queue(self, file_id: str, task_id: str, file_type: str, priority: int = 0) -> bool:
        """
        将文件添加到处理队列（检查重复）
        """
        try:
            # 检查文件是否已经在处理中
            if self.is_file_processing(file_id):
                logger.warning(f"文件 {file_id} 已在处理中，跳过重复提交")
                return False
            
            logger.info(f"🚀 提交文件处理任务: {file_id}")
            result = process_review_file.delay(file_id, task_id, file_type)
            
            # 标记文件为处理中
            self.set_file_status(file_id, "submitted", {
                "celery_task_id": result.id,
                "task_id": task_id,
                "file_type": file_type,
                "processing": True
            })
            
            logger.info(f"✅ 文件任务已提交，Celery任务ID: {result.id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ 提交文件任务失败: {e}")
            return False
    
    def is_task_processing(self, task_id: str) -> bool:
        """检查任务是否正在处理中"""
        lock_key = f"task_lock:{task_id}"
        return self.cache_redis.exists(lock_key) > 0
    
    def is_file_processing(self, file_id: str) -> bool:
        """检查文件是否正在处理中"""
        lock_key = f"file_lock:{file_id}"
        return self.cache_redis.exists(lock_key) > 0
    
    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """获取任务状态"""
        try:
            key = f"task_status:{task_id}"
            status_json = self.cache_redis.get(key)
            
            if status_json:
                return json.loads(status_json)
            return None
            
        except Exception as e:
            logger.error(f"获取任务状态失败: {e}")
            return None
    
    def set_task_status(self, task_id: str, status: str, extra_data: Dict = None) -> bool:
        """设置任务状态"""
        try:
            status_data = {
                "status": status,
                "updated_at": datetime.utcnow().isoformat()
            }
            
            if extra_data:
                status_data.update(extra_data)
            
            key = f"task_status:{task_id}"
            self.cache_redis.setex(key, 86400, json.dumps(status_data))
            return True
            
        except Exception as e:
            logger.error(f"设置任务状态失败: {e}")
            return False
    
    def get_file_status(self, file_id: str) -> Optional[Dict]:
        """获取文件状态"""
        try:
            key = f"file_status:{file_id}"
            status_json = self.cache_redis.get(key)
            
            if status_json:
                return json.loads(status_json)
            return None
            
        except Exception as e:
            logger.error(f"获取文件状态失败: {e}")
            return None
    
    def set_file_status(self, file_id: str, status: str, extra_data: Dict = None) -> bool:
        """设置文件状态"""
        try:
            status_data = {
                "status": status,
                "updated_at": datetime.utcnow().isoformat()
            }
            
            if extra_data:
                status_data.update(extra_data)
            
            key = f"file_status:{file_id}"
            self.cache_redis.setex(key, 86400, json.dumps(status_data))
            return True
            
        except Exception as e:
            logger.error(f"设置文件状态失败: {e}")
            return False
    
    def update_progress(self, entity_id: str, progress: int, message: str = "") -> bool:
        """更新处理进度"""
        try:
            progress_data = {
                "progress": max(0, min(100, progress)),
                "message": message,
                "updated_at": datetime.utcnow().isoformat()
            }
            
            key = f"progress:{entity_id}"
            self.cache_redis.setex(key, 3600, json.dumps(progress_data))
            return True
            
        except Exception as e:
            logger.error(f"更新进度失败: {e}")
            return False
    
    def get_progress(self, entity_id: str) -> Optional[Dict]:
        """获取处理进度"""
        try:
            key = f"progress:{entity_id}"
            progress_json = self.cache_redis.get(key)
            
            if progress_json:
                return json.loads(progress_json)
            return None
            
        except Exception as e:
            logger.error(f"获取进度失败: {e}")
            return None
    
    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态统计"""
        try:
            
            inspect = current_app.control.inspect()
            active_tasks = inspect.active()
            reserved_tasks = inspect.reserved()
            
            total_active = sum(len(tasks) for tasks in (active_tasks or {}).values())
            total_reserved = sum(len(tasks) for tasks in (reserved_tasks or {}).values())
            
            # 统计锁信息
            task_locks = len(self.cache_redis.keys("task_lock:*"))
            file_locks = len(self.cache_redis.keys("file_lock:*"))
            
            return {
                "active_tasks": total_active,
                "reserved_tasks": total_reserved,
                "total_pending": total_active + total_reserved,
                "workers_online": len(active_tasks or {}),
                "task_locks": task_locks,
                "file_locks": file_locks,
                "queue_info": "使用 Celery + Redis 分布式锁"
            }
            
        except Exception as e:
            logger.error(f"获取队列状态失败: {e}")
            return {
                "active_tasks": 0,
                "reserved_tasks": 0,
                "total_pending": 0,
                "workers_online": 0,
                "task_locks": 0,
                "file_locks": 0,
                "error": str(e)
            }
    
    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        try:
            # 获取 Celery 任务ID
            task_status = self.get_task_status(task_id)
            if not task_status or "celery_task_id" not in task_status:
                return False
            
            # 取消 Celery 任务
            current_app.control.revoke(task_status["celery_task_id"], terminate=True)
            
            # 释放锁
            lock_key = f"task_lock:{task_id}"
            self.cache_redis.delete(lock_key)
            
            # 更新状态
            self.set_task_status(task_id, "cancelled")
            
            logger.info(f"✅ 任务已取消: {task_id}")
            return True
            
        except Exception as e:
            logger.error(f"取消任务失败: {e}")
            return False
    
    def test_connection(self) -> bool:
        """测试连接"""
        try:
            self.redis.ping()
            self.cache_redis.ping()
            logger.info("✅ QueueService 连接测试成功")
            return True
            
        except Exception as e:
            logger.error(f"❌ QueueService 连接测试失败: {e}")
            return False