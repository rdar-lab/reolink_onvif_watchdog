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
#   CAMERA_PASSWORD              — global fallback
#   CAMERA_PASSWORD_<NAME>       — per-camera (name in uppercase)

ENTRYPOINT ["python", "watchdog.py"]
CMD ["config.yaml"]
