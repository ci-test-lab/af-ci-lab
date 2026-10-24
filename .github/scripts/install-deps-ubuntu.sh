#!/usr/bin/env bash
# Installs the packages for an ArrayFire CPU build, the docs or the lint check
# on Ubuntu 22.04, 24.04 or 26.04. Runs as root or through sudo.
#
# Usage: install-deps-ubuntu.sh [--compiler gcc|clang|icx] [--blas openblas|mkl]
#                               [--cmake <version>] [--docs] [--lint]
set -euo pipefail

oneapi_version=2026.2
doxygen_version=1.9.7
doxygen_sha256=8bcb7cbdb9d8ae78d0bf65a383bc533db5232e2527ab10743f1e58cba92a7d43
declare -A cmake_sha256=(
  [3.16.3]=3e15dadfec8d54eda39c2f266fc1e571c1b88bf32f9d221c8a039b07234206fa
)

compiler=
blas=
cmake_version=
docs=
lint=
while [[ $# -gt 0 ]]; do
  case $1 in
    --compiler) compiler=$2; shift 2 ;;
    --blas) blas=$2; shift 2 ;;
    --cmake) cmake_version=$2; shift 2 ;;
    --docs) docs=1; shift ;;
    --lint) lint=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

sudo=
[[ $(id -u) -eq 0 ]] || sudo=sudo
export DEBIAN_FRONTEND=noninteractive

packages=(ca-certificates curl git)
[[ -n $lint ]] && packages+=(python3 python3-venv)
if [[ -n $compiler || -n $docs ]]; then
  packages+=(build-essential cmake ninja-build libboost-dev)
fi
if [[ -n $compiler ]]; then
  packages+=(ccache pkg-config libfftw3-dev liblapacke-dev libfreeimage-dev
             libglfw3-dev libfreetype-dev libfontconfig1-dev)
fi
case $compiler in
  gcc|icx|'') ;;
  clang) packages+=(clang) ;;
  *) echo "unknown compiler: $compiler" >&2; exit 2 ;;
esac
case $blas in
  mkl|'') ;;
  openblas) packages+=(libopenblas-dev) ;;
  *) echo "unknown BLAS: $blas" >&2; exit 2 ;;
esac
[[ $compiler == icx || $blas == mkl ]] && packages+=(gnupg)

$sudo apt-get update -q
$sudo apt-get install -y -q --no-install-recommends "${packages[@]}"

if [[ $compiler == icx || $blas == mkl ]]; then
  keyring=/usr/share/keyrings/oneapi-archive-keyring.gpg
  curl -fsSL https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
    | gpg --dearmor | $sudo tee $keyring > /dev/null
  echo "deb [signed-by=$keyring] https://apt.repos.intel.com/oneapi all main" \
    | $sudo tee /etc/apt/sources.list.d/oneAPI.list > /dev/null
  oneapi_packages=("intel-oneapi-mkl-devel-$oneapi_version")
  [[ $compiler == icx ]] && oneapi_packages+=("intel-oneapi-compiler-dpcpp-cpp-$oneapi_version")
  $sudo apt-get update -q
  $sudo apt-get install -y -q "${oneapi_packages[@]}"
fi

# Downloads a release archive, checks its SHA-256 and unpacks it into /opt/<name>.
install_archive() {
  local url=$1 sha256=$2 name=$3 tmp
  tmp=$(mktemp)
  curl -fsSL -o "$tmp" "$url"
  echo "$sha256  $tmp" | sha256sum -c --quiet
  $sudo mkdir -p "/opt/$name"
  $sudo tar -xzf "$tmp" -C "/opt/$name" --strip-components 1
  rm -f "$tmp"
}

if [[ -n $cmake_version ]]; then
  sha=${cmake_sha256[$cmake_version]:-}
  [[ -n $sha ]] || { echo "no checksum for CMake $cmake_version" >&2; exit 2; }
  install_archive \
    "https://github.com/Kitware/CMake/releases/download/v$cmake_version/cmake-$cmake_version-Linux-x86_64.tar.gz" \
    "$sha" "cmake-$cmake_version"
  "/opt/cmake-$cmake_version/bin/cmake" --version | head -n 1
fi

if [[ -n $docs ]]; then
  install_archive \
    "https://github.com/doxygen/doxygen/releases/download/Release_${doxygen_version//./_}/doxygen-$doxygen_version.linux.bin.tar.gz" \
    "$doxygen_sha256" "doxygen-$doxygen_version"
  $sudo ln -sf "/opt/doxygen-$doxygen_version/bin/doxygen" /usr/local/bin/doxygen
  echo "doxygen $(doxygen --version)"
fi
