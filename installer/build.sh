#!/bin/bash
# Builds Memory-Lane-Setup.exe from the ../app folder (needs: apt install nsis)
set -e
cd "$(dirname "$0")"
rm -rf stage && mkdir -p stage/app
S=../app
cp "$S"/photokeeper.py "$S"/ui.html "$S"/memorylane.ico "$S"/README.txt "$S"/requirements*.txt stage/app/
cp -r "$S"/licenses stage/app/
V=$(grep -m1 '^VERSION' "$S"/photokeeper.py | sed 's/.*"\(.*\)".*/\1/')
makensis -V2 -DVERSION="$V" memorylane.nsi
ls -la Memory-Lane-Setup.exe
