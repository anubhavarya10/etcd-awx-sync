FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY main.py .

# Copy etcd_to_awx sync script and debug tools
COPY etcd_to_awx.py ./etcd-awx-sync/
COPY debug_awx_auth.py ./etcd-awx-sync/

# Set environment variables
ENV PYTHONPATH="/app:/app/etcd-awx-sync"
ENV PYTHONUNBUFFERED=1

# Health check port
EXPOSE 8080

# Run the agent
CMD ["python", "main.py"]
