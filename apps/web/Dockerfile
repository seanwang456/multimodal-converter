FROM node:20-alpine

WORKDIR /app
RUN npm install -g pnpm

ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_TELEMETRY_DISABLED=1

COPY package.json ./
RUN pnpm install
COPY . .
RUN pnpm build

EXPOSE 3000
CMD ["pnpm", "start"]
