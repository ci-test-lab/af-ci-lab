#!/usr/bin/env bash
# Installs the Homebrew packages for an ArrayFire CPU build on macOS. BLAS and
# LAPACK come from Apple's Accelerate framework; CMake 3.25 or newer must
# already be installed.
set -euo pipefail

brew install boost ccache fftw fontconfig freeimage glfw ninja pkgconf
