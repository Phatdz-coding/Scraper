#!/usr/bin/env bash
set -e

REPO="Phatdz-coding/Scraper"
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

if [ "$OS" = "linux" ] && [ "$ARCH" = "x86_64" ]; then
  ASSET="scrape-linux-x64.tar.gz"
elif [ "$OS" = "darwin" ] && [ "$ARCH" = "arm64" ]; then
  ASSET="scrape-macos-arm64.tar.gz"
else
  echo "Unsupported operating system: $OS ($ARCH)"
  exit 1
fi

URL="https://github.com/$REPO/releases/latest/download/$ASSET"
TMP_DIR=$(mktemp -d)

echo "Downloading $ASSET..."
curl -fsSL "$URL" -o "$TMP_DIR/$ASSET"
tar -xzf "$TMP_DIR/$ASSET" -C "$TMP_DIR"

echo "Installing to /usr/local/bin/scrape..."
sudo install -m 755 "$TMP_DIR/scrape" /usr/local/bin/scrape
rm -rf "$TMP_DIR"

echo "Installation successful! Try running: scrape --help"