#!/usr/bin/env bash
# Build "Agent Cockpit.app" into ~/Applications so the dashboard behaves like a
# real macOS app — launchable from Spotlight/Dock, with its own icon and an
# app-style (chromeless) window. Idempotent: safe to re-run. No sudo, never
# touches /Applications.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="$REPO_DIR/assets"
APP_DIR="$HOME/Applications/Agent Cockpit.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
PORT=8457
URL="http://127.0.0.1:$PORT/"

echo "Building $APP_DIR"

# --- 0. Clean rebuild of the bundle skeleton ---------------------------------
rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES"

# --- 1. Info.plist ------------------------------------------------------------
cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Agent Cockpit</string>
    <key>CFBundleDisplayName</key>     <string>Agent Cockpit</string>
    <key>CFBundleIdentifier</key>      <string>com.rhmiyakawa.agent-cockpit</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundleIconFile</key>        <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>  <string>10.13</string>
    <key>LSUIElement</key>             <false/>
    <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

# --- 2. Launcher executable ---------------------------------------------------
# Health-checks the dashboard, restarts it if down, then opens an app-style
# window (Chrome --app if available, else the default browser).
cat > "$MACOS/launcher" <<LAUNCHER
#!/usr/bin/env bash
set -u
URL="$URL"
RESTART="\$HOME/ghostty-launcher/restart-ghostty-launcher.sh"

# 1. Health check; restart the dashboard service if it's not responding.
if ! curl -fs --max-time 2 "\$URL" >/dev/null 2>&1; then
  if [ -x "\$RESTART" ]; then
    "\$RESTART" >/dev/null 2>&1 || true
  fi
  # Wait (up to ~6s) for it to come up.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    curl -fs --max-time 2 "\$URL" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

# 2. Open the dashboard as an app-style window.
#    If a cockpit window is already open in Chrome, focus it instead of
#    spawning a duplicate. Only touch Chrome via AppleScript when it's already
#    running — a 'tell' would otherwise auto-launch a full Chrome just to look.
CHROME="/Applications/Google Chrome.app"
if [ -d "\$CHROME" ]; then
  FOCUSED=0
  if pgrep -x "Google Chrome" >/dev/null 2>&1; then
    FOCUSED=\$(osascript <<'APPLESCRIPT'
on run
    set matchNeedle to "127.0.0.1:8457"
    try
        tell application "Google Chrome"
            if (count of windows) is 0 then return "0"
            repeat with w in windows
                repeat with t in (tabs of w)
                    try
                        if (URL of t as string) contains matchNeedle then
                            set index of w to 1
                            activate
                            return "1"
                        end if
                    end try
                end repeat
            end repeat
        end tell
    end try
    return "0"
end run
APPLESCRIPT
)
  fi
  # No existing cockpit window (or Chrome wasn't running): create the app
  # window. The -n is correct here — with none open, it reliably makes the
  # chromeless --app window (--app is ignored if Chrome is already running).
  if [ "\$FOCUSED" != "1" ]; then
    open -na "Google Chrome" --args --app="\$URL"
  fi
else
  open "\$URL"
fi
LAUNCHER
chmod +x "$MACOS/launcher"

# --- 3. App icon (.icns) from the 512 PNG ------------------------------------
if [ ! -f "$ASSETS_DIR/icon-512.png" ]; then
  echo "ERROR: $ASSETS_DIR/icon-512.png not found — run the icon generator first." >&2
  exit 1
fi

ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
# Required iconset members, downscaled from the 512 master with sips.
sips -z 16 16     "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_16x16.png"       >/dev/null
sips -z 32 32     "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_16x16@2x.png"    >/dev/null
sips -z 32 32     "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_32x32.png"       >/dev/null
sips -z 64 64     "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_32x32@2x.png"    >/dev/null
sips -z 128 128   "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_128x128.png"     >/dev/null
sips -z 256 256   "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_128x128@2x.png"  >/dev/null
sips -z 256 256   "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_256x256.png"     >/dev/null
sips -z 512 512   "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_256x256@2x.png"  >/dev/null
sips -z 512 512   "$ASSETS_DIR/icon-512.png" --out "$ICONSET/icon_512x512.png"     >/dev/null
cp "$ASSETS_DIR/icon-512.png"                 "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

# Nudge Finder/LaunchServices to pick up the new bundle + icon.
touch "$APP_DIR"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP_DIR" >/dev/null 2>&1 || true

echo "✓ Built $APP_DIR"
echo "  Launch:  open \"$APP_DIR\""
