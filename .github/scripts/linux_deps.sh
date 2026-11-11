#!/bin/bash
# Linux libraries for Qt (and with --exiftool also exiftool) without waiting on Ubuntu's apt mirrors, which at
# times hang for many minutes: most libraries are already on GitHub's image, so apt runs only for missing ones
# (and refreshes its lists only if that fails); exiftool, a Perl program, comes straight from its repository.
set -euo pipefail
# package -> the library Qt loads from it. The libraries themselves are checked: a package can count as installed
# while its library is not there for this architecture.
declare -A libraries=(
  [libegl1]=libEGL.so.1 [libgl1]=libGL.so.1 [libxkbcommon0]=libxkbcommon.so.0
  [libdbus-1-3]=libdbus-1.so.3 [libfontconfig1]=libfontconfig.so.1 [libxcb-cursor0]=libxcb-cursor.so.0
)
has_library() {   # not "ldconfig -p | grep -q": with pipefail, grep stopping early fails the pipe
  local known
  known=$(ldconfig -p)
  grep -qF "$1 (libc6,x86-64)" <<< "$known"
}
missing=()
for package in "${!libraries[@]}"; do
  has_library "${libraries[$package]}" || missing+=("$package")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "::notice title=Qt libraries::installing ${missing[*]}"
  apt="sudo timeout 300 apt-get -o Acquire::Retries=3"
  $apt install -y --no-install-recommends "${missing[@]}" \
    || { $apt update && $apt install -y --no-install-recommends "${missing[@]}"; }
  for package in "${missing[@]}"; do
    has_library "${libraries[$package]}" || { echo "::error::${libraries[$package]} still missing"; exit 1; }
  done
else
  echo "All Qt libraries are present"
fi
if [ "${1:-}" = "--exiftool" ]; then
  version=13.55
  curl -fsSL --retry 3 "https://github.com/exiftool/exiftool/archive/refs/tags/$version.tar.gz" | tar xz -C "$RUNNER_TEMP"
  echo "$RUNNER_TEMP/exiftool-$version" >> "$GITHUB_PATH"
  "$RUNNER_TEMP/exiftool-$version/exiftool" -ver
fi
