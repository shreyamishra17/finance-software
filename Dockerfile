FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CATEGORIZER_MODEL_DIR=/app/categorizer/models/bert_distilbert_50k_full
# Tell Playwright where to store browser binaries inside the image
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System libraries required by Playwright's bundled Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libcups2 libdbus-1-3 libexpat1 libfontconfig1 libglib2.0-0 \
    libgtk-3-0 libnspr4 libx11-6 libx11-xcb1 libxcb1 \
    libxcursor1 libxi6 libxtst6 fonts-liberation wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's bundled Chromium (used for Smart Connect headless automation)
RUN playwright install chromium

COPY . /app

ENV PORT=8000
EXPOSE 8000

CMD ["python", "src/serve_demo.py"]
