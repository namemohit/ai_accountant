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

# Command to run.
# --ws-max-size 268435456 (256MB): the seed_baseline Import response ships all
# vouchers + ledgers + raw XML in one WebSocket frame; real books exceed the
# 16MB uvicorn default and get rejected with close code 1009 "message too
# big". Keep this in sync with the ws_max_size in server.py's uvicorn.run().
CMD uvicorn server:app --host 0.0.0.0 --port $PORT --ws-max-size 268435456
