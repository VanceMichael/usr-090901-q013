# 具身智能实验资源排程服务 —— 纯后台镜像
# 多阶段：test 阶段在构建期执行自动化测试，最终镜像只保留运行时。

FROM python:3.12-alpine AS test
WORKDIR /app
COPY requirements-dev.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app ./app
COPY fixtures ./fixtures
COPY contracts ./contracts
COPY tests ./tests
RUN python -m pytest -q

FROM python:3.12-alpine AS runtime
WORKDIR /app
# 数字 UID：容器内非 root 运行
RUN addgroup -S app && adduser -S -G app app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY fixtures ./fixtures
COPY contracts ./contracts
USER app
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8080"]
