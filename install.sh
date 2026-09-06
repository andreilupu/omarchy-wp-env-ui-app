#!/bin/bash
#
# Dev/git-checkout install: symlinks the wp-env scripts into ~/.local/bin,
# then runs wp-env-ui-setup for the per-user pieces (systemd user service,
# launcher entry, bar widget). Safe to re-run — re-running also restarts the
# server so it picks up updated code.
#
# Arch users can install the package from packaging/PKGBUILD instead and then
# run wp-env-ui-setup once.

set -euo pipefail

REPO_DIR="$(dirname "$(readlink -f "$0")")"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$BIN_DIR"

for script in "$REPO_DIR"/bin/*; do
  chmod +x "$script"
  ln -sf "$script" "$BIN_DIR/$(basename "$script")"
  echo "Linked $(basename "$script") -> $BIN_DIR"
done

"$BIN_DIR/wp-env-ui-setup"

echo
echo "Done. Open the app from the launcher (SUPER + SPACE → \"wp-env\") or"
echo "register a pinned site with: wp-env-register-site <slug> \"Display Name\""
