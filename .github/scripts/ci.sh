#!/usr/bin/env bash
# Runs one CPU CI job: installs its dependencies, then runs its CMake workflow
# preset or check. CI runs it inside the job's container image; --docker runs
# it the same way in a fresh local container.
#
# Usage: ci.sh [--docker] [--no-deps] <job>
#        ci.sh --list
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
scripts=$root/.github/scripts
floor_cmake=3.16.3

# job, container image, arguments for the dependency script
jobs="
lint                          ubuntu:24.04  --lint
docs                          ubuntu:24.04  --docs
linux-gcc-openblas            ubuntu:24.04  --compiler gcc --blas openblas
linux-clang-openblas          ubuntu:24.04  --compiler clang --blas openblas
linux-icx-mkl                 ubuntu:24.04  --compiler icx --blas mkl
linux-gcc-openblas-floor      ubuntu:22.04  --compiler gcc --blas openblas --cmake $floor_cmake
linux-gcc-openblas-next       ubuntu:26.04  --compiler gcc --blas openblas
macos-arm64-clang-accelerate  -
windows-msvc-openblas         -
"

usage() {
  echo "usage: $0 [--docker] [--no-deps] <job>" >&2
  echo "       $0 --list" >&2
  exit 2
}

docker=
deps=1
while [[ $# -gt 0 ]]; do
  case $1 in
    --list) echo "$jobs" | awk 'NF { print $1, ($2 == "-" ? "(host)" : $2) }'; exit 0 ;;
    --docker) docker=1; shift ;;
    --no-deps) deps=; shift ;;
    -*) usage ;;
    *) break ;;
  esac
done
[[ $# -eq 1 ]] || usage
job=$1
spec=$(echo "$jobs" | awk -v job="$job" '$1 == job')
[[ -n $spec ]] || { echo "unknown job: $job (see --list)" >&2; exit 2; }
read -r _ image deps_args <<< "$spec"

cd "$root"

if [[ -n $docker ]]; then
  [[ $image != - ]] || { echo "$job runs on the host, not in a container" >&2; exit 2; }
  # The repository is mounted, so build trees land in build/ as usual; give them
  # back to the calling user afterwards.
  # shellcheck disable=SC2086
  exec docker run --rm ${AF_CI_DOCKER_ARGS:-} \
    -v "$root:/src" -w /src \
    -v "${AF_CI_CCACHE_VOLUME:-arrayfire-ccache}:/ccache" -e CCACHE_DIR=/ccache \
    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/src \
    -e CMAKE_BUILD_PARALLEL_LEVEL -e AF_LINT_BASE \
    "$image" bash -c '.github/scripts/ci.sh "$1"; rc=$?
      chown -R "$2" build docs/details/examples.dox 2> /dev/null; exit $rc' \
    bash "$job" "$(id -u):$(id -g)"
fi

if [[ $job == windows-* ]]; then
  export VCPKG_ROOT=${VCPKG_ROOT:-$HOME/vcpkg}
fi

if [[ -n $deps ]]; then
  # shellcheck disable=SC2086
  case $job in
    macos-*) "$scripts/install-deps-macos.sh" ;;
    windows-*) pwsh -NoProfile -File "$scripts/install-deps-windows.ps1" ;;
    *) "$scripts/install-deps-ubuntu.sh" $deps_args ;;
  esac
fi

case $job in
  lint)
    "$scripts/lint.sh"
    ;;
  linux-icx-mkl)
    set +eu
    # shellcheck disable=SC1091
    source /opt/intel/oneapi/setvars.sh --force > /dev/null
    set -eu
    cmake --workflow --preset "ci-$job"
    ;;
  linux-gcc-openblas-floor)
    # CMake 3.16 cannot read presets; these settings mirror the
    # ci-linux-gcc-openblas preset.
    build=build/ci-$job
    "/opt/cmake-$floor_cmake/bin/cmake" -S . -B "$build" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
      -DAF_BUILD_CPU=ON -DAF_BUILD_CUDA=OFF -DAF_BUILD_OPENCL=OFF \
      -DAF_BUILD_ONEAPI=OFF -DAF_BUILD_UNIFIED=OFF -DAF_BUILD_FORGE=ON \
      -DAF_BUILD_EXAMPLES=ON -DAF_BUILD_DOCS=OFF -DAF_TEST_WITH_MTX_FILES=OFF \
      -DAF_COMPUTE_LIBRARY=FFTW/LAPACK/BLAS
    "/opt/cmake-$floor_cmake/bin/cmake" --build "$build"
    # The distribution's ctest runs the same tests and can write JUnit.
    OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 ctest --test-dir "$build" \
      -j 2 --output-on-failure --no-tests=error --output-junit "$root/$build/junit.xml"
    ;;
  *)
    cmake --workflow --preset "ci-$job"
    ;;
esac
