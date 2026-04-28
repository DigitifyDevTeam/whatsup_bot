# Bridge: Node.js WhatsApp listener (Baileys) running ts-node
FROM node:20-slim

ENV NODE_ENV=production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./

# Install full deps (incl. dev) so ts-node + typescript are available at runtime
RUN npm install --include=dev

COPY tsconfig.json ./
COPY src ./src

RUN mkdir -p /app/logs /app/auth_state /app/audio_files

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command (overridden by docker-compose to also tee logs to file)
CMD ["npx", "ts-node", "src/index.ts"]
