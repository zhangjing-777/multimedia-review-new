FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（包含文档和视频处理）
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    # PDF处理依赖
    poppler-utils \
    # 视频处理依赖
    ffmpeg \
    libsm6 \
    libxext6 \
    libfontconfig1 \
    libxrender1 \
    libgl1-mesa-glx \
    # 图像处理依赖
    libjpeg-dev \
    libpng-dev \
    # 清理
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/

# 创建必要目录
RUN mkdir -p uploads logs

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]