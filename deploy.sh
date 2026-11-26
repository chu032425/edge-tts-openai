#!/usr/bin/env bash
set -euo pipefail

BASE_URL="https://raw.githubusercontent.com/chu032425/edge-tts-openai/main"
INSTALL_DIR="${1:-edge-tts-openai}"

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "正在下载 docker-compose.yml ..."
curl -fsSL -o docker-compose.yml "$BASE_URL/docker-compose.yml"

echo "正在下载 .env ..."
curl -fsSL -o .env "$BASE_URL/.env"

read -rp "请输入 API_KEY（直接回车则使用默认值）: " API_KEY_INPUT

if [[ -n "$API_KEY_INPUT" ]]; then
  awk -v key="$API_KEY_INPUT" '
    /^API_KEY=/ { print "API_KEY=" key; replaced=1; next }
    { print }
    END { if (!replaced) print "API_KEY=" key }
  ' .env > .env.tmp
  mv .env.tmp .env
fi

echo "正在启动服务 ..."
docker compose up -d
