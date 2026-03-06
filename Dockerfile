FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY watchdog.py .
COPY config.yaml .

# The config file path can be overridden at runtime via CMD or by mounting
# a custom config at /app/config.yaml.
#
# Passwords are injected via environment variables (never baked into the image):
#   CAMERA_PASSWORD   — global fallback
#   CAMERA_1          — password for the 1st camera in config.yaml
#   CAMERA_2          — password for the 2nd camera in config.yaml
#   (and so on for each camera by its 1-based position in the config)

ENTRYPOINT ["python", "watchdog.py"]
CMD ["config.yaml"]
