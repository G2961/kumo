#!/bin/sh
# Fast local build + install on the connected device
set -e
cd "$(dirname "$0")"
../.local/gradle-8.9/bin/gradle assembleRelease -q
# re-discover the phone when the wifi-adb endpoint went stale (port changes on reboot)
if ! adb devices | grep -q '24301FDF600063'; then
 echo "Phone not connected — searching Wireless debugging..."
 EP=$(adb mdns services | grep '24301FDF600063' | grep 'tls-connect' | awk '{print $NF}' | head -1)
 if [ -n "$EP" ];then adb connect "$EP" || true;fi
fi
DEV=$(adb devices | awk '/device$/{print $1}' | grep '24301FDF600063' | head -1)
if [ -n "$DEV" ];then ADB="adb -s $DEV";else ADB="adb";fi
$ADB install -r app/build/outputs/apk/release/app-release.apk
$ADB shell am force-stop space.g2961.opencode
sleep 1
$ADB shell am start -n space.g2961.opencode/.MainActivity
echo "Installed."
