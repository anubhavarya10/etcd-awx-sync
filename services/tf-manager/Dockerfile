FROM python:3.11-slim

WORKDIR /app

# Install git for gitpython
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY main.py .

# Create workspace for git operations
RUN mkdir -p /app/workspace && chmod 755 /app/workspace

# Expose port
EXPOSE 8083

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8083/health')" || exit 1

# Run
CMD ["python", "main.py"]
