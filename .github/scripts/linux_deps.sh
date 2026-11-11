#!/bin/bash
# Linux libraries for Qt (and with --exiftool also exiftool) without waiting on Ubuntu's apt mirrors, which at
# times hang for many minutes: most libraries are already on GitHub's image, so apt runs only for missing ones
# (and refreshes its lists only if that fails); exiftool, a Perl program, comes straight from its repository.
set -euo pipefail
packages=(libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libxcb-cursor0)
missing=()
for package in "${packages[@]}"; do
  # "install ok installed": dpkg -s also succeeds for a removed package whose config files are left
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing+=("$package")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Installing ${missing[*]}"
  apt="sudo timeout 300 apt-get -o Acquire::Retries=3"
  $apt install -y --no-install-recommends "${missing[@]}" \
    || { $apt update && $apt install -y --no-install-recommends "${missing[@]}"; }
else
  echo "All Qt libraries are present"
fi
if [ "${1:-}" = "--exiftool" ]; then
  version=13.55
  curl -fsSL --retry 3 "https://github.com/exiftool/exiftool/archive/refs/tags/$version.tar.gz" | tar xz -C "$RUNNER_TEMP"
  echo "$RUNNER_TEMP/exiftool-$version" >> "$GITHUB_PATH"
  "$RUNNER_TEMP/exiftool-$version/exiftool" -ver
fi
