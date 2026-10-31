set(VCPKG_TARGET_ARCHITECTURE x64)
# Since vcpkg 2026.07.29, vcpkg_find_fortran installs MinGW gfortran (for the LAPACK
# port) only when the triplet asks for it, as vcpkg's own x64-windows does.
set(VCPKG_PROVIDED_FORTRAN ON)

if(PORT MATCHES "freetype")
  set(VCPKG_CRT_LINKAGE static)
  set(VCPKG_LIBRARY_LINKAGE static)
else()
  set(VCPKG_CRT_LINKAGE dynamic)
  set(VCPKG_LIBRARY_LINKAGE dynamic)
endif()
