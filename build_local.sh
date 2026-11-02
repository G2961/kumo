#!/bin/sh
# Fast local build + install on the connected device
set -e
cd "$(dirname "$0")"
../.local/gradle-8.9/bin/gradle assembleRelease -q
adb install -r app/build/outputs/apk/release/app-release.apk
adb shell am force-stop space.g2961.opencode
sleep 1
adb shell am start -n space.g2961.opencode/.MainActivity
echo "Installed."
