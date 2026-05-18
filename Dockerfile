# Use official Python image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install psycopg2-binary

# Copy all files
COPY . .

# Expose port (Cloud Run uses PORT env var)
ENV PORT 8080

# Command to run
CMD uvicorn server:app --host 0.0.0.0 --port $PORT
