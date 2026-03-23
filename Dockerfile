FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (Docker layer cache — only rebuilds if requirements change)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create directories for runtime data
RUN mkdir -p /app/logs /app/data

# Expose web dashboard port
EXPOSE 8765

# Default command: run both bot + web server via the launcher script
CMD ["python3", "launcher.py"]
