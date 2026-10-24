# Contributing to ArrayFire #

## Reporting Issues ##

We both value and encourage the community to provide feedback about using ArrayFire and the issues they are facing.
The more detailed the information, the easier it is for the developers to resolve the issue.

Issues can span a variety of topics including:
- Feature requests
- Bug reports
- Build Issues
- Performance improvements
- New hardware / backend support

We use the github [issue tracker](https://github.com/arrayfire/arrayfire/issues?state=open) to track our issues. Technical issues can also be discussed on our [user forum](https://groups.google.com/forum/#!forum/arrayfire-users).

## Contributing Code ##

If you want to contribute code, we suggest you use the one of the following methods.

- [Using Github](https://github.com/arrayfire/arrayfire/wiki/Contribute-code-using-github)
- [Using Email](https://github.com/arrayfire/arrayfire/wiki/Contribute-code-using-email)

Key areas of code contributions include:
- [New features](https://github.com/arrayfire/arrayfire/issues?q=is%3Aopen+is%3Aissue+label%3Afeature)
    - You can refer to [this](https://github.com/arrayfire/arrayfire/wiki/How-to-Add-a-New-Function-to-ArrayFire) wiki page to get started on how to add a new function to the library.
- [Bug fixes](https://github.com/arrayfire/arrayfire/labels/bug)
- [Style changes](https://github.com/arrayfire/arrayfire/labels/style)
- [Performance improvements](https://github.com/arrayfire/arrayfire/labels/style)
- [New tests](https://github.com/arrayfire/arrayfire/labels/test)
- New examples!

## Running the CI Checks Locally ##

Each CPU check that CI runs is a job of `.github/scripts/ci.sh` (`--list` shows them). With
Docker, a Linux job runs in the same Ubuntu image that CI uses:

    .github/scripts/ci.sh --docker lint
    .github/scripts/ci.sh --docker docs
    .github/scripts/ci.sh --docker linux-gcc-openblas

The build tree goes to `build/<preset>` and test results to `build/<preset>/junit.xml`. Set
`CMAKE_BUILD_PARALLEL_LEVEL` to limit build parallelism.

Without `--docker`, `ci.sh` first installs the job's packages with apt, Homebrew or vcpkg;
`--no-deps` skips that. The build itself is a CMake workflow preset, which needs CMake 3.25 or
newer, for example:

    cmake --workflow --preset ci-linux-gcc-openblas

`lint` checks the formatting of the lines you changed since the default branch with
clang-format 15.0.7.

##ArrayFire Based Projects##

You can also contribute to ArrayFire by helping out projects that use ArrayFire! For our part, in addition to the ArrayFire library we are also in the process of adding native bindings for numerous language. We currently support

- [Java](https://github.com/arrayfire/arrayfire_java)
- [R](https://github.com/arrayfire/arrayfire_r)
- [Fortran](https://github.com/arrayfire/arrayfire_fortran)

If you are experienced in any of these languages, you can help us improve these language bindings. If you prefer a different language that is not in the list, dive in and create a new repo!
