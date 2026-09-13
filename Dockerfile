# 语法版本由基础镜像固定为 Python 3.12。
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# 一次性验收镜像：在运行时镜像之上追加测试依赖，供 compose 的 verify 服务使用。
FROM base AS verify

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini ./pytest.ini

CMD ["python", "scripts/acceptance.py"]
