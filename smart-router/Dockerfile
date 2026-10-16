# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Akshay Smart Router — Author: Akshay Dubey
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ROUTER_ROOT=/app ROUTER_DB=/data/router.db \
    ROUTER_HOME=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY router ./router
COPY config ./config
COPY skills ./skills
# Generated code is tested inside this container as an unprivileged user.
RUN useradd -m router && mkdir -p /data && chown router /data
USER router
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["uvicorn", "router.api:app", "--host", "0.0.0.0", "--port", "8080"]
