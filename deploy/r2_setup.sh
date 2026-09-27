#!/usr/bin/env bash
# Publish the story-page tile archives (docs/tiles/*.pmtiles) to Cloudflare R2.
# Prerequisites: R2 enabled on the Cloudflare account, and `npx wrangler login` done once.
# Usage (from repo root): bash deploy/r2_setup.sh [bucket-name]
set -euo pipefail
BUCKET="${1:-houston1947-tiles}"
WR="npx --yes wrangler@4"

$WR whoami
$WR r2 bucket create "$BUCKET" || echo "bucket exists? continuing"
$WR r2 bucket cors set "$BUCKET" --file deploy/r2-cors.json --force
# Public access via the rate-limited r2.dev URL (fine for a low-traffic research page).
# For production, attach a custom domain instead: $WR r2 bucket domain add "$BUCKET" --domain tiles.example.com --zone-id <zone>
$WR r2 bucket dev-url enable "$BUCKET" --force || true
for f in docs/tiles/*.pmtiles; do
  $WR r2 object put "$BUCKET/$(basename "$f")" --file "$f" --remote \
      --content-type application/octet-stream --cache-control "public, max-age=86400"
done
$WR r2 bucket dev-url get "$BUCKET"
