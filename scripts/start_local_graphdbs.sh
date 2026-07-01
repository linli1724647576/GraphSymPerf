#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

docker compose -f infra/docker-compose.graphdbs.yml up -d
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
