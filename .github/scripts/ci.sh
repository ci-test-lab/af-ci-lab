#!/usr/bin/env bash
# Runs one CI job: installs its dependencies, then runs its CMake workflow
# preset or check. CI runs it inside the job's container image; --docker runs
# it the same way in a fresh local container.
#
# A job builds and tests the source tree in AF_CI_SOURCE_DIR (default: this
# repository) with this repository's scripts and CI settings, so it can test any
# commit. For the linux-cuda-gpu-* jobs, AF_CI_CUDA_VERSION (default 12.9) and
# AF_CI_CUDA_ARCHITECTURES (default 7.5;8.9) select the toolkit and the GPU
# architectures. With AF_CI_CLEAN_EXPORT set, --docker builds a copy of the
# tree's tracked files without .git instead of mounting the tree itself.
# It must run on macOS's bash 3.2: the macOS job runs it on the host.
#
# Usage: ci.sh [--docker] [--no-deps] <job>
#        ci.sh --list
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
scripts=$root/.github/scripts
floor_cmake=3.16.3
cuda_version=${AF_CI_CUDA_VERSION:-12.9}

# job, container image, arguments for the dependency script
jobs="
lint                          ubuntu:24.04  --lint
docs                          ubuntu:24.04  --docs
workflows                     ubuntu:24.04  --workflows
linux-gcc-openblas            ubuntu:24.04  --compiler gcc --blas openblas
linux-clang-openblas          ubuntu:24.04  --compiler clang --blas openblas
linux-icx-mkl                 ubuntu:24.04  --compiler icx --blas mkl
linux-gcc-openblas-floor      ubuntu:22.04  --compiler gcc --blas openblas --cmake $floor_cmake
linux-gcc-openblas-next       ubuntu:26.04  --compiler gcc --blas openblas
linux-cuda                    ubuntu:24.04  --compiler gcc --cuda 12.9
linux-opencl                  ubuntu:24.04  --compiler gcc --blas openblas --opencl
linux-oneapi                  ubuntu:24.04  --compiler icx --blas mkl --opencl --oneapi 2025.3
linux-cuda-gpu-build          ubuntu:24.04  --compiler gcc --cuda $cuda_version
linux-cuda-gpu-test           ubuntu:24.04  --compiler gcc --cuda $cuda_version
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
source=$(cd "${AF_CI_SOURCE_DIR:-$root}" && pwd)
gpu_build=$source/build/ci-linux-cuda-gpu
if [[ -n ${AF_CI_CLEAN_EXPORT:-} && -z $docker ]]; then
  echo "AF_CI_CLEAN_EXPORT needs --docker: a build on the host could reach the tree's credentials" >&2
  exit 2
fi

if [[ -n $docker ]]; then
  [[ $image != - ]] || { echo "$job runs on the host, not in a container" >&2; exit 2; }
  # ccache lives in a named volume, or in a host directory (AF_CI_CCACHE_DIR)
  # that CI can save between runs.
  ccache=${AF_CI_CCACHE_VOLUME:-arrayfire-ccache}
  if [[ -n ${AF_CI_CCACHE_DIR:-} ]]; then
    mkdir -p "$AF_CI_CCACHE_DIR"
    ccache=$(cd "$AF_CI_CCACHE_DIR" && pwd)
  fi
  # Ninja's default (CPUs + 2) can run out of memory. Unless CMAKE_BUILD_PARALLEL_LEVEL
  # is set, run as many compiles as fit the container's --memory limit, or 3/4 of
  # Docker's memory without one: about 2.5 GiB each for SYCL and 0.75 GiB for the
  # rest. Never more than the CPUs, nor 8, the most every job was measured with.
  # Jobs that compile nothing skip this.
  if [[ -z ${CMAKE_BUILD_PARALLEL_LEVEL:-} && ! $job =~ ^(lint|docs|workflows|linux-cuda-gpu-test)$ ]]; then
    read -r cpus mib <<< "$(docker info --format '{{.NCPU}} {{.MemTotal}}')"
    mib=$((mib * 3 / 4 / 1048576))
    memory="3/4 of Docker's memory"
    args=$(printf ' %s ' "${AF_CI_DOCKER_ARGS:-}" | tr '[:upper:]' '[:lower:]')
    if [[ $args =~ [[:space:]](--memory|-m)[=[:space:]]*([0-9]+)([kmg]?)b?[[:space:]] ]]; then
      case ${BASH_REMATCH[3]} in
        g) mib=$((BASH_REMATCH[2] * 1024)) ;;
        m) mib=${BASH_REMATCH[2]} ;;
        k) mib=$((BASH_REMATCH[2] / 1024)) ;;
        *) mib=$((BASH_REMATCH[2] / 1048576)) ;;
      esac
      memory="the --memory limit"
    fi
    [[ $args =~ [[:space:]]--cpus[=[:space:]]*([0-9]+) ]] && cpus=${BASH_REMATCH[1]}
    per_compile=768 per_compile_gib=0.75
    [[ $job == linux-oneapi ]] && per_compile=2560 per_compile_gib=2.5
    parallel=$((mib / per_compile))
    ((parallel > cpus)) && parallel=$cpus
    ((parallel > 8)) && parallel=8
    ((parallel < 1)) && parallel=1
    export CMAKE_BUILD_PARALLEL_LEVEL=$parallel
    echo "Building with $parallel parallel jobs: $mib MiB ($memory) / $per_compile_gib GiB" \
      "per compile = $((mib / per_compile)), $cpus CPUs, at most 8." \
      "Set CMAKE_BUILD_PARALLEL_LEVEL to override."
  fi
  # A working tree that may hold credentials, such as the one an agent edits and
  # pushes from, is never mounted. The build gets a copy of its tracked files
  # without .git (no untracked files either), in a new directory under
  # AF_CI_EXPORT_DIR, so it can neither read the tree's git config nor change the
  # tree. The lint job gets the diff of the changed lines, computed here.
  lint_diff=()
  if [[ -n ${AF_CI_CLEAN_EXPORT:-} ]]; then
    export_dir=$(mktemp -d "${AF_CI_EXPORT_DIR:-${TMPDIR:-/tmp}}/arrayfire-ci.XXXXXX")
    mkdir "$export_dir/src"
    git --no-optional-locks -C "$source" ls-files -z --cached |
      (cd "$source" && while IFS= read -r -d '' file; do
         if [[ -e $file || -L $file ]]; then printf '%s\0' "$file"; fi
       done) |
      tar -C "$source" --null --no-recursion -T - -cf - | tar -C "$export_dir/src" -xf -
    if [[ $job == lint ]]; then
      (cd "$source" && "$scripts/lint.sh" --diff) > "$export_dir/lint.diff"
      lint_diff=(-v "$export_dir/lint.diff:/lint.diff:ro" -e AF_LINT_DIFF=/lint.diff)
    fi
    echo "Building a copy of $source without .git; the results stay in $export_dir/src"
    source=$export_dir/src
  fi
  # The repository is mounted, so build trees land in build/ as usual; give them
  # and a ccache directory back to the calling user afterwards. A source tree from
  # elsewhere, and always the GPU jobs' one, is mounted at /src, with this
  # repository read-only at /ci.
  mounts=(-v "$root:/src")
  script=.github/scripts/ci.sh
  if [[ $job == linux-cuda-gpu-* || $source != "$root" ]]; then
    mounts=(-v "$root:/ci:ro" -v "$source:/src" -e AF_CI_SOURCE_DIR=/src
            -e AF_CI_CUDA_VERSION -e AF_CI_CUDA_ARCHITECTURES)
    script=/ci/.github/scripts/ci.sh
    [[ $job == *-test ]] && mounts+=(--gpus all)
  fi
  # shellcheck disable=SC2086
  exec docker run --rm ${AF_CI_DOCKER_ARGS:-} "${mounts[@]}" ${lint_diff[@]+"${lint_diff[@]}"} -w /src \
    -v "$ccache:/ccache" -e CCACHE_DIR=/ccache -e CCACHE_MAXSIZE \
    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/src \
    -e CMAKE_BUILD_PARALLEL_LEVEL -e AF_LINT_BASE \
    "$image" bash -c '"$1" "$2"; rc=$?
      chown -R "$3" build docs/details/examples.dox ${4:+/ccache} 2> /dev/null; exit $rc' \
    bash "$script" "$job" "$(id -u):$(id -g)" "${AF_CI_CCACHE_DIR:-}"
fi

if [[ $job == linux-cuda-gpu-test ]] && ! nvidia-smi; then
  echo "No NVIDIA GPU is visible, and $job needs one (docker run --gpus all)" >&2
  exit 1
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

# A source tree from elsewhere gets this repository's presets, which hold the CI
# settings, so commits that predate them can be tested the same way.
if [[ $source != "$root" && $job != linux-cuda-gpu-* ]]; then
  cp "$root/CMakePresets.json" "$source/CMakePresets.json"
  echo "Building $source with the presets of $root"
fi
cd "$source"

case $job in
  lint)
    if [[ -n ${AF_LINT_DIFF:-} ]]; then
      "$scripts/lint.sh" --check "$AF_LINT_DIFF"
    else
      "$scripts/lint.sh"
    fi
    ;;
  linux-icx-mkl|linux-oneapi)
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
      -j 2 --output-on-failure --no-tests=error --output-junit "$source/$build/junit.xml"
    ;;
  workflows)
    # actionlint also runs shellcheck on the workflows' run: steps.
    actionlint
    shellcheck .github/scripts/*.sh
    for test in .github/scripts/*-test.py; do
      python3 "$test"
    done
    ;;
  linux-cuda-gpu-build)
    cmake -S "$source" -B "$gpu_build" -G Ninja -C "$scripts/linux-cuda-gpu.cmake" \
      "-DCUDA_architecture_build_targets=${AF_CI_CUDA_ARCHITECTURES:-7.5;8.9}"
    cmake --build "$gpu_build"
    # The test job gets what the tests need, to unpack at the same path: test
    # programs, shared libraries, CTest files and test data.
    (cd "$source" && find "${gpu_build#"$source/"}" \( -name CMakeFiles -o -name .git \) -prune -o \
      \( -name CTestTestfile.cmake -o -name '*.so*' -o -path '*/test/*' -type f -perm -u+x \
         -o -path '*/extern/af_test_data-src/*' \) \( -type f -o -type l \) -print) |
      tar -C "$source" --zstd -cf "$gpu_build.tar.zst" -T -
    ls -l "$gpu_build.tar.zst"
    ;;
  linux-cuda-gpu-test)
    [[ -d $gpu_build ]] || tar -C "$source" --zstd -xf "$gpu_build.tar.zst"
    OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 ctest --test-dir "$gpu_build" \
      --timeout 900 --output-on-failure --no-tests=error \
      --output-junit "$gpu_build/junit.xml" 2>&1 | tee "$gpu_build/ctest.log"
    ;;
  *)
    cmake --workflow --preset "ci-$job"
    ;;
esac
