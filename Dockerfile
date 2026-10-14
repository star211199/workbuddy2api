FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 转换器（单文件部署模式）
COPY converter.py responses_adapter.py anthropic_adapter.py desensitize.py ./
COPY upstream_compat.py responses_projection.py ./

# 管理后台：docker-compose 的 admin 服务执行 `uvicorn admin.server:app`，
# 必须把 admin 包和静态资源一起拷进来，否则容器内 `ModuleNotFoundError: No module named 'admin'`，
# 在 restart: unless-stopped 下会无限崩溃重启。
COPY admin ./admin
COPY main.py ./

# 注意：不要 `COPY . .` —— .env / logs / __pycache__ 不应进镜像（见 .dockerignore）。

EXPOSE 8787 8790

CMD ["python3", "converter.py", "--host", "0.0.0.0", "--port", "8787", "--skip-check"]
