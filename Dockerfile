FROM python:3.13-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建输出和日志目录
RUN mkdir -p output logs

# 生成自动词汇表层（glossary.auto.json 不入库，构建时拉取；失败时降级为仅手写词表）
RUN python tools/sync_glossary.py || echo "skip: 自动词表生成失败，将只使用 glossary.json"

CMD ["python", "scheduler.py"]
