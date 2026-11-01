FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRACE_REQUIRE_LOGIN=1 TRACE_DATA=/var/lib/trace-ai PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN mkdir -p /var/lib/trace-ai /var/data
EXPOSE 8000
CMD ["python", "cloud_server.py"]
