# 采用最新的 Python 轻量化底包
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制依赖配置并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY bot.py .

# 启动运行
CMD ["python", "bot.py"]