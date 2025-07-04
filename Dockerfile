# Use an official Python slim image
FROM python:3.10-slim

# Install system dependencies (LibreOffice, fonts, and others)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        fonts-dejavu \
        fonts-liberation \
        ttf-mscorefonts-installer \
        poppler-utils \
        unoconv \
        curl \
        && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose the port (if needed by Render)
EXPOSE 5000

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# Start the Flask app
CMD ["python", "app.py"]