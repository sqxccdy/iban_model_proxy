# Author: Mason sun
# Create Datetime: 2026-07-09
# Build: docker build -f Dockerfile --platform=linux/amd64 -t origin-hub-ai-registry.cn-shanghai.cr.aliyuncs.com/dataflow/model_proxy:v1.0.0 .
FROM library/python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

RUN mkdir -p /app
WORKDIR /app

COPY ./requirements.txt ./
COPY ./model.yaml ./
COPY ./src ./src
RUN pip3 install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/

CMD ["gunicorn", "-w", "4", "-k", "aiohttp.GunicornWebWorker", "-b", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", "src.main:app"]
#CMD ["tail", "-f", "/dev/null"]
