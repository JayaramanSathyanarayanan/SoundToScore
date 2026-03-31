FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    fluidsynth \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set base directory
WORKDIR /app

# Copy project
COPY . .

# Move into backend folder
WORKDIR /app/soundforge/backend

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 10000

# Start FastAPI
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
