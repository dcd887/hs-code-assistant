FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt -i https://mirrors.ustc.edu.cn/pypi/simple/

# 复制代码和数据（保持与本地一致的目录结构）
COPY backend/main.py ./backend/
COPY data/ ./data/

WORKDIR /app/backend

# 环境变量（运行时通过云托管控制台配置，不硬编码API Key）
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
