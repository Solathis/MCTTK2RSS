FROM python:3.13-slim

# curl_cffi 需要 libcurl 和 libssl
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4-openssl-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建输出和日志目录
RUN mkdir -p output logs

CMD ["python", "scheduler.py"]
