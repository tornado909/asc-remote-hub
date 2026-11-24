#!/bin/bash
set -euo pipefail
cd /src
B=/sdk/build-tools/35.0.0
A=/sdk/platforms/android-35/android.jar
mkdir -p /out /out/classes /out/dex
rm -rf /out/classes /out/dex
mkdir -p /out/classes /out/dex
"$B/aapt2" compile --dir res -o /out/res.zip
"$B/aapt2" link -I "$A" --manifest AndroidManifest.xml -o /out/resources.apk /out/res.zip
javac -source 8 -target 8 -encoding UTF-8 -cp "$A" -d /out/classes src/*.java
"$B/d8" --lib "$A" --min-api 30 --output /out/dex $(find /out/classes -name '*.class')
cp /out/resources.apk /out/unsigned.apk
(cd /out/dex && jar uf /out/unsigned.apk classes.dex)
"$B/zipalign" -f 4 /out/unsigned.apk /out/aligned.apk
umask 077
if [ ! -f /keys/release.p12 ]; then
  head -c 48 /dev/urandom | base64 -w0 > /keys/password
  keytool -genkeypair -alias asc-android -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=ASC Remote Hub' -storetype PKCS12 -keystore /keys/release.p12 -storepass:file /keys/password
fi
"$B/apksigner" sign --ks /keys/release.p12 --ks-pass file:/keys/password --out /out/asc-remote-hub-android.apk /out/aligned.apk
"$B/apksigner" verify --verbose /out/asc-remote-hub-android.apk
"$B/aapt2" dump badging /out/asc-remote-hub-android.apk | head -15
