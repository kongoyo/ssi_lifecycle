FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python requirements
COPY .github/workflows/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install specific playwright browser instance
RUN playwright install chromium

# Copy source code and config files
COPY . .

# Run the python script
CMD ["python", "ssi_v2.py"]
