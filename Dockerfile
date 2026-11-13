FROM python:3.13-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建输出和日志目录
RUN mkdir -p output logs

# 注意：自动词表（glossary.auto.json / glossary.auto.index.json）不在这里生成。
# 它由宿主机手动执行 tools/sync_glossary.py 生成后以只读方式挂载进容器，
# 避免构建或运行时联网下载术语表，网络不通时不会卡住启动。

CMD ["python", "scheduler.py"]
