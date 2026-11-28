#!/usr/bin/env bash
set -euo pipefail
# Run inside the dedicated non-root user's graphical session.
if [[ $EUID -eq 0 ]]; then echo "Do not run as root; use a dedicated kiosk user" >&2; exit 1; fi
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null
command -v systemctl >/dev/null
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "Install the distro's Chromium package before continuing." >&2; exit 1
fi
install -d -m 0700 "$HOME/.local/lib/clinic-flow" "$HOME/.config/clinic-flow" "$HOME/.config/systemd/user"
install -m 0700 "$here/kiosk_supervisor.py" "$HOME/.local/lib/clinic-flow/kiosk_supervisor.py"
install -m 0644 "$here/clinic-flow-kiosk.service" "$HOME/.config/systemd/user/clinic-flow-kiosk.service"
if [[ ! -f "$HOME/.config/clinic-flow/kiosk.env" ]]; then
  install -m 0600 "$here/kiosk.env.example" "$HOME/.config/clinic-flow/kiosk.env"
fi
systemctl --user daemon-reload
echo "Edit ~/.config/clinic-flow/kiosk.env, then run:"
echo "  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY"
echo "  systemctl --user enable --now clinic-flow-kiosk.service"
echo "If graphical-session.target is not started by your desktop, use its autostart mechanism."
