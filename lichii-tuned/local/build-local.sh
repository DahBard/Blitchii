#!/usr/bin/env bash
# Builds a tuned Lichii on this laptop the way CI does (lichii-tuned.yml, pgo-lto), from the
# tuned4 source worktree, and packages it into the builds folder.
# Run in an MSYS2 UCRT64 shell:
#   bash lichii-tuned/local/build-local.sh <tuned4 source dir> <builds folder> <label> [pgo-lto|pgo]
# e.g. bash lichii-tuned/local/build-local.sh /c/lx/lichii-src "/c/Users/me/Forked Lichii Builds" l001
# The package lands in "<builds folder>/<label>"; an existing folder is never overwritten.
set -euo pipefail
src=$(cd "$1" && pwd)
builds=$2
label=$3
variant=${4:-pgo-lto}
here=$(cd "$(dirname "$0")/.." && pwd)        # the tuned checkout's lichii-tuned folder
out="$builds/$label"
[ -e "$out" ] && { echo "build-local: $out exists; pick another label" >&2; exit 1; }
export PATH="/ucrt64/bin:$PATH"
export CCACHE_DIR="${CCACHE_DIR:-$src/.ccache}" CCACHE_MAXSIZE=8000M CCACHE_COMPILERCHECK=content
tune="-march=x86-64-v3 -mtune=arrowlake -ffp-contract=off"
cd "$src"

# Name the build in the log and title bar, as CI does: <commit> and tuned-<label>-<variant>.
commit=$(git rev-parse HEAD)
printf '%s' "$commit" > GIT-COMMIT
printf '%s' "tuned-$label-$variant" > GIT-REFSPEC

# Keep the symbol table (CI does the same to its checkout); the file stays modified, hidden from git status.
file=externals/cmake-modules/DetectPlatform.cmake
if grep -q -e '--strip-all' "$file"; then
  sed -i 's/-Wl,--strip-all //' "$file"
  git update-index --skip-worktree "$file"
fi

# PGO profile: Lichii's own, merged with the owner's recorded ones when there are any.
profdata="$PWD/eglantine.profdata"
if command -v gh >/dev/null && [ "$variant" != "pgo-train" ]; then
  GITHUB_TOKEN=$(gh auth token 2>/dev/null || true) GITHUB_REPOSITORY=DahBard/Blitchii \
    python "$here/fetch_pgo_profiles.py" "$here/pgo" || true
fi
shopt -s nullglob
raws=("$here"/pgo/*.profraw "$here"/pgo/*/*.profraw)
if [ ${#raws[@]} -gt 0 ]; then
  mkdir -p build/pgo-last
  inputs=(--weighted-input=1,eglantine.profdata)
  n=0
  for raw in "${raws[@]}"; do
    n=$((n + 1)); last="build/pgo-last/$n-${raw##*/}"
    if python -I "$here/profraw_last_dump.py" --llvm-profdata "$(cygpath -m "$(command -v llvm-profdata)")" "$raw" "$last"; then
      inputs+=("--weighted-input=70,$last")
    elif llvm-profdata show "$raw" >/dev/null 2>&1; then
      inputs+=("--weighted-input=4,$raw")
    fi
  done
  if [ ${#inputs[@]} -gt 1 ] && llvm-profdata merge --failure-mode=all -o merged.profdata "${inputs[@]}"; then
    profdata="$PWD/merged.profdata"
  fi
fi
profile="-fprofile-use=$(cygpath -m "$profdata") -Wno-backend-plugin -Wno-profile-instr-unprofiled -Wno-profile-instr-out-of-date"
[ "$variant" = "pgo-train" ] && profile="-fprofile-generate"

linker="-fuse-ld=lld"
extra=()
lto=OFF
if [ "$variant" = "pgo-lto" ]; then
  lto=ON
  mkdir -p .thinlto
  linker="$linker -Wl,--thinlto-cache-dir=$(cygpath -m "$PWD/.thinlto")"
  for tool in AR:llvm-ar RANLIB:llvm-ranlib; do
    path=$(cygpath -m "$(command -v "${tool#*:}")")
    extra+=("-DCMAKE_${tool%%:*}=${path%.exe}.exe")
  done
fi
bdir="build/$variant"
cmake -S . -B "$bdir" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DLICHII_TESTS=OFF -DLICHII_CMD=OFF \
  -DLICHII_ROOM=OFF -DLICHII_ROOM_STANDALONE=OFF -DLICHII_USE_BUNDLED_QT=OFF \
  -DLICHII_USE_BUNDLED_SDL3=ON -DLICHII_USE_BUNDLED_FFMPEG=ON \
  -DVulkanHeaders_FORCE_BUNDLED=ON -DVulkanUtilityLibraries_FORCE_BUNDLED=ON \
  -DENABLE_UPDATE_CHECKER=OFF -DUSE_DISCORD_PRESENCE=OFF -DUSE_CCACHE=ON -DENABLE_LTO=$lto \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  "-DCMAKE_EXE_LINKER_FLAGS=$linker" "-DCMAKE_C_FLAGS=$profile $tune" "-DCMAKE_CXX_FLAGS=$profile $tune" \
  "${extra[@]}"
cmake --build "$bdir" --target lichii --parallel -- -k 0

# Package into a staging folder, then rename it to the label (as CI's Package step).
stage="$builds/.staging-$label"
rm -rf "$stage"; mkdir -p "$stage"
cp -r "$bdir/bin/." "$stage/"
cd "$stage"
mkdir -p symbols && cp lichii.exe symbols/lichii.exe && cp "$here/symbolize_crash.py" symbols/ 2>/dev/null || true
llvm-objcopy --strip-all lichii.exe
windeployqt6 --no-translations --no-compiler-runtime --no-opengl-sw --no-system-dxc-compiler --no-system-d3d-compiler lichii.exe
deps() {
  objdump -p "$1" | awk '/DLL Name:/ {print $3}' | while read -r dll; do
    [ -z "$dll" ] && continue
    source=$(command -v "$dll" 2>/dev/null || true)
    [ -z "$source" ] && continue
    case "$source" in *System32* | *SysWOW64*) continue ;; esac
    if [ ! -f "$dll" ]; then cp "$source" "$dll"; deps "$source"; fi
  done
}
deps lichii.exe
find . -name '*.dll' | while read -r dll; do deps "$dll"; done
cp -r "$src"/dist/lichii-tuned/* .
mkdir -p recipes && cp "$here"/recipes/*.nx2recipe recipes/
base=https://raw.githubusercontent.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/7539ae8b6568ad29ed78a753404351daf3b5cca7/Kits/FidelityFX
fetch() { curl -fsSL --retry 4 --retry-delay 2 -o "$2" "$base/$1"; echo "$3  $2" | sha256sum -c -; }
fetch signedbin/amd_fidelityfx_loader_vk.dll amd_fidelityfx_loader_vk.dll 38a56a01fb3565c0757d9f5c1e4e69de4b65e5f2e5769b323b200dc1f142bab9
fetch signedbin/amd_fidelityfx_upscaler_vk.dll amd_fidelityfx_upscaler_vk.dll 4a7e895af6e0b615d40a84d59eb8c2b1054ad91cdd0c739567940270d51bdab7
fetch signedbin/amd_fidelityfx_framegeneration_vk.dll amd_fidelityfx_framegeneration_vk.dll d002dabff8c5e71eaa715fc99438cd4dc81230262a7897b16bc402efec3498b4
fetch docs/license.md amd_fidelityfx_license.md 32357259b965d6a8001e9e508ba60eda6034027c5923a7d226ffbaff813c2bfb
printf '%s\n' "label=$label" "variant=$variant" "commit=$commit" > build-local.txt
cd "$builds"
mv "$stage" "$out"
echo "build-local: $out (commit ${commit:0:10}, $variant)"
