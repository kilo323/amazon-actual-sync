FROM python:3.12-slim

# Build args for user/group IDs (defaults: 99/100)
ARG APP_UID=1000
ARG APP_GID=1000

# Install Node.js (LTS) and npm alongside system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 libgtk-3-0 libx11-xcb1 \
    wget ca-certificates fonts-liberation curl gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Create a non-root user and the data directory
RUN groupadd -g ${APP_GID} appgroup \
    && useradd -m -u ${APP_UID} -g ${APP_GID} appuser \
    && mkdir -p /data \
    && chown ${APP_UID}:${APP_GID} /data

WORKDIR /app

# Install playwright package first in isolation so the browser download
# layer is cached independently of requirements.txt.  Changing other
# dependencies will no longer re-trigger the slow browser download.
RUN pip install --no-cache-dir playwright

# Download browsers (this layer is cached as long as the playwright
# version above doesn't change, regardless of other pip deps)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
RUN playwright install chromium --with-deps 2>/dev/null || \
    playwright install chromium 

# Install all Python dependencies (may upgrade/pin playwright again; that's fine)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Wrap every Chromium binary found in /ms-playwright to inject --no-sandbox
# (required when running as a non-root user inside a Docker container)
RUN for bin in $(find /ms-playwright -type f \( -name 'chrome' -o -name 'chromium' \)); do \
      mv "$bin" "${bin}.real" && \
      printf '#!/bin/sh\nexec "%s.real" --no-sandbox "$@"\n' "$bin" > "$bin" && \
      chmod +x "$bin"; \
    done

# Copy Node.js package files and install dependencies
COPY package.json ./
RUN npm install --no-package-lock

# Copy application source
COPY app/ ./app/
COPY webapp/ ./webapp/
COPY actual-budget-api.js ./

USER appuser

EXPOSE 8080

CMD ["python", "-m", "webapp"]
