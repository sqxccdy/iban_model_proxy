# Author: Mason sun
# Create Datetime: 2026-07-09
FROM library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

RUN mkdir -p /app
WORKDIR /app

COPY ./requirements.txt ./
COPY ./src ./src
RUN pip3 install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/

CMD ["gunicorn", "-w", "2", "-k", "aiohttp.GunicornWebWorker", "-b", "0.0.0.0:8000", "src.main:app"]
#CMD ["tail", "-f", "/dev/null"]
