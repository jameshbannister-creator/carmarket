#!/bin/bash
# Weekly scrape runner — called by LaunchAgent every Monday 09:30
# Runs the scraper then pushes updated data to GitHub

set -e

REPO="/Users/james/Downloads/carmarket"
SCRAPER="$REPO/scraper"
SSH_KEY="$HOME/.ssh/github_carmarket"

cd "$SCRAPER"
/usr/bin/python3 run_all.py

cd "$REPO"
git add data/latest.json data/snapshots/
git diff --cached --quiet && exit 0  # nothing changed, skip push

git commit -m "data: weekly snapshot $(date +%Y-%m-%d)"
GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  git push origin main
