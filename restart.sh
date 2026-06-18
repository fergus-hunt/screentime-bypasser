#!/usr/bin/env bash
echo "Stopping the container..."
docker compose down

echo "Pulling latest changes from GitHub..."
git pull

echo "Rebuilding and starting the container..."
docker compose up -d --build

echo "Done! The container is now running with the latest code."
