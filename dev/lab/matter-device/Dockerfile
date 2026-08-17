FROM node:22-bookworm-slim

WORKDIR /app

# Official Matter.js development light used as the lab's synthetic node.
RUN npm install --global --no-audit --no-fund @matter/examples@0.15.6

ENTRYPOINT ["matter-device"]
