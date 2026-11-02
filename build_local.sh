#!/bin/sh
# Fast local build + install on the connected device
set -e
cd "$(dirname "$0")"
../.local/gradle-8.9/bin/gradle assembleRelease -q
echo "Build OK"
# find OUR phone (match by serialno: wifi transports show as ip:port without the serial)
DEV=""
for D in $(adb devices | awk '/device$/{print $1}'); do
 SN=$(adb -s "$D" shell getprop ro.serialno 2>/dev/null | tr -d '\r')
 if [ "$SN" = "24301FDF600063" ];then DEV="$D";break;fi
done
if [ -z "$DEV" ];then
 echo "Phone not connected — searching Wireless debugging..."
 SVC=$(adb mdns services | grep '24301FDF600063' | grep 'tls-connect' | awk '{print $NF}' | head -1)
 if [ -n "$SVC" ];then adb connect "$SVC" || true;sleep 1;DEV="$SVC";fi
fi
if [ -z "$DEV" ];then echo "Phone not found: unlock it, keep screen ON and Wireless debugging ON (or plug USB).";exit 1;fi
echo "Phone: $DEV"
APK=app/build/outputs/apk/release/app-release.apk
# streamed install hangs with adbd over wifi; push + pm install is reliable and shows progress
MSYS_NO_PATHCONV=1 adb -s "$DEV" push "$APK" /data/local/tmp/oc.apk
adb -s "$DEV" shell "pm install -r /data/local/tmp/oc.apk; rm -f /data/local/tmp/oc.apk; am force-stop space.g2961.opencode; sleep 1; am start -n space.g2961.opencode/.MainActivity"
echo "Installed."
