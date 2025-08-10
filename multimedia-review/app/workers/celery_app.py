"""
简化的 Celery 配置
去除复杂的队列配置，使用默认队列
"""

from celery import Celery
from app.config import get_settings

# 获取配置
settings = get_settings()

# 创建 Celery 实例
celery_app = Celery(
    "review_center",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.review_worker"]
)

# 配合分布式锁机制的 Celery 配置
celery_app.conf.update(
    # 基础配置
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # 使用默认队列（保持简单）
    task_default_queue="celery",
    task_default_exchange="celery",
    task_default_routing_key="celery",
    
    # 关键配置：配合分布式锁
    worker_prefetch_multiplier=1,   # 每次只取一个任务（防止积压）
    task_acks_late=True,           # 任务完成后才确认（防止丢失）
    task_reject_on_worker_lost=True, # worker 丢失时拒绝任务（防止重复）
    
    # 任务超时配置
    task_soft_time_limit=1800,     # 软超时30分钟
    task_time_limit=2100,          # 硬超时35分钟
    
    # 重试配置（配合锁机制，减少重试）
    task_default_retry_delay=60,   # 重试延迟60秒
    task_max_retries=2,           # 减少到2次重试（因为有锁保护）
    
    # 结果配置
    result_expires=86400,          # 结果保存24小时
    
    # 监控配置
    worker_send_task_events=True,
    task_send_sent_event=True,
    
    # 任务路由（保持默认）
    task_routes={
        'app.workers.review_worker.*': {'queue': 'celery'},
    },
    
    # 任务去重配置（配合Redis锁）
    task_ignore_result=False,      # 保留结果用于状态跟踪
    task_store_eager_result=True,  # 立即存储结果
    
    # Worker 行为配置
    worker_disable_rate_limits=True, # 禁用速率限制（锁已提供控制）
    worker_max_tasks_per_child=100,  # 每个子进程最多处理100个任务后重启
    
    # 连接配置
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
)

# 日志配置
celery_app.conf.update(
    worker_log_format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s',
)
