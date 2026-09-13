# 应用镜像：FastAPI + 原生前端（无前端构建步骤）
#
# 构建（必须在**项目根**执行，构建上下文就是项目根）：
#   docker build -t agent-platform:v1.0.0 -f deploy/Dockerfile .
#
# 可选依赖：默认装「默认入库后端 + 默认文档解析」；不需要就传空串关掉：
#   --build-arg EXTRAS=""
# 需要额外的再追加，例如接 OpenAI 做向量化、或把平台库换成 PostgreSQL：
#   --build-arg EXTRAS="pymilvus>=3.0.0 pypdf>=4.0.0 python-docx>=1.1.0 openai>=1.30.0 psycopg2-binary>=2.9.9"
FROM python:3.12-slim

ARG EXTRAS="pymilvus>=3.0.0 pypdf>=4.0.0 python-docx>=1.1.0"

# PYTHONDONTWRITEBYTECODE：配合 K8s 的只读根文件系统，避免写 __pycache__ 失败
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# slim 镜像只有 UTC，装 tzdata 让容器内时间与 TZ 一致（日志 / 脚本场景）
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ -n "$EXTRAS" ]; then pip install --no-cache-dir $EXTRAS; fi

# 只拷贝运行所需内容；镜像里没有 .env（配置一律用环境变量注入）
COPY app ./app
COPY config ./config
COPY ext_plugins ./ext_plugins
COPY scripts ./scripts
COPY tests ./tests
COPY web ./web

# 数据目录：平台的 SQLite 库、上传原件、脚本工作区都落在这里，生产环境挂数据卷
RUN mkdir -p data/uploads data/script_workspace \
 && useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查走免登录的 /api/health，与 K8s 探针同一入口
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# 必须监听 0.0.0.0：run.py 里的 127.0.0.1 只适合本机直跑，容器里外部访问不到
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
