#!/usr/bin/env python3
"""Standalone OpenUSD build driver (no SCons).

Ported from fabric-flow-openusd/scons/openusd.py: clones OpenUSD at the pinned
version, patches build_usd.py for the VS2026 generator, and runs OpenUSD's own
build_usd.py with the monolithic / no-python runtime flags. Driven by the
Makefile (elixir_make) so the whole thing stays Elixir-native.

Usage:
    python3 build_openusd.py --build-dir <out> [--with-python] [--release]

Env:
    OPENUSD_VERSION   USD version/tag without the leading 'v' (default 26.05)
    USE_SCCACHE       when truthy, route OpenUSD's CMake compiles through sccache
"""
import argparse
import gzip
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile


def platform_name():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def patch_vs2026(open_usd_path):
    """Add VS2026 generator support to the cloned build_usd.py (idempotent)."""
    build_usd = os.path.join(open_usd_path, "build_scripts", "build_usd.py")
    if not os.path.isfile(build_usd):
        return
    with open(build_usd, encoding="utf-8") as f:
        src = f.read()
    if "IsVisualStudio2026OrGreater" in src:
        return

    patch = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "patches", "openusd-vs2026-generator.patch")
    if os.path.isfile(patch):
        if subprocess.run(["git", "apply", "-p1", patch], cwd=open_usd_path).returncode == 0:
            return

    patched = src.replace(
        "def IsVisualStudio2022OrGreater():",
        "def IsVisualStudio2026OrGreater():\n"
        "    VISUAL_STUDIO_2026_VERSION = (14, 50)\n"
        "    return IsVisualStudioVersionOrGreater(VISUAL_STUDIO_2026_VERSION)\n"
        "def IsVisualStudio2022OrGreater():",
        1,
    ).replace(
        '        if IsVisualStudio2022OrGreater():\n'
        '            generator = "Visual Studio 17 2022"',
        '        if IsVisualStudio2026OrGreater():\n'
        '            generator = "Visual Studio 18 2026"\n'
        '        elif IsVisualStudio2022OrGreater():\n'
        '            generator = "Visual Studio 17 2022"',
        1,
    )
    if patched != src:
        with open(build_usd, "w", encoding="utf-8") as f:
            f.write(patched)


def build_python_info(plat):
    """(executable, include_dir, library, version) for --build-python-info,
    pointing LIBRARY at the shared libpython that actually exists (conda's
    sysconfig otherwise reports a static .a it doesn't ship)."""
    version = sysconfig.get_config_var("py_version_short")
    include_dir = sysconfig.get_path("include")
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    if plat == "windows":
        nodot = sysconfig.get_config_var("py_version_nodot")
        candidates = [os.path.join(sys.base_prefix, "libs", f"python{nodot}.lib")]
    elif plat == "macos":
        candidates = [os.path.join(libdir, f"libpython{version}.dylib")]
    else:
        candidates = [os.path.join(libdir, f"libpython{version}.so")]
    library = next((c for c in candidates if os.path.exists(c)), candidates[0])
    return [sys.executable, include_dir, library, version]


def windows_msvc_env():
    """Return the environment set by vcvars64.bat so build_usd.py finds cl."""
    vswhere = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    if not os.path.exists(vswhere):
        raise RuntimeError("vswhere.exe not found")
    vs_path = subprocess.check_output(
        [vswhere, "-latest", "-products", "*",
         "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"], encoding="utf-8").strip()
    if not vs_path:
        raise RuntimeError("No Visual Studio with the required VC tools found")
    vcvars = os.path.join(vs_path, "VC", "Auxiliary", "Build", "vcvars64.bat")
    output = subprocess.check_output(f'"{vcvars}" >nul && set', shell=True, text=True)
    env = {}
    for line in output.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.upper()] = v
    return env


# --- Windows llvm-mingw (x86_64-windows-gnu) build --------------------------
#
# The default path uses OpenUSD's build_usd.py, which forces the Visual Studio
# (MSVC) generator on Windows. That produces an MSVC-ABI usd_ms, which cannot be
# linked by llvm-mingw consumers (e.g. cloth-fit's NIF). This path builds a
# GNU-ABI usd_ms with llvm-mingw instead: drive USD's CMake directly (no
# build_usd.py), against a shared oneTBB, with the small openusd-mingw.patch.

_TBB_TAG = "v2021.9.0"  # matches the oneTBB cloth-fit's NIF uses (ABI/version parity)


def _run(cmd, **kw):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def apply_mingw_patch(src):
    patch = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "patches", "openusd-mingw.patch")
    # Idempotent: skip if it already applies in reverse (i.e. already applied).
    if subprocess.run(["git", "apply", "--reverse", "--check", patch],
                      cwd=src, capture_output=True).returncode == 0:
        print("openusd-mingw.patch already applied", flush=True)
        return
    _run(["git", "apply", patch], cwd=src)


def build_onetbb_shared_mingw(prefix, cc, cxx, make):
    """Build a SHARED oneTBB with llvm-mingw. usd_ms and the consumer NIF must
    share one TBB runtime instance; two static instances abort at first use."""
    src = "onetbb-src"
    if not os.path.isdir(src):
        _run(["git", "clone", "-b", _TBB_TAG, "--depth", "1",
              "https://github.com/oneapi-src/oneTBB.git", src])
    bld = "onetbb-build"
    _run(["cmake", "-S", src, "-B", bld, "-G", "MinGW Makefiles",
          f"-DCMAKE_MAKE_PROGRAM={make}",
          f"-DCMAKE_C_COMPILER={cc}", f"-DCMAKE_CXX_COMPILER={cxx}",
          "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=ON",
          "-DTBB_TEST=OFF", "-DTBB_STRICT=OFF",
          f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"])
    _run(["cmake", "--build", bld, "-j", str(os.cpu_count() or 4), "--target", "install"])


def build_windows_gnu(src, build_dir, release, sccache_args):
    cc = shutil.which("clang") or "clang"
    cxx = shutil.which("clang++") or "clang++"
    make = shutil.which("mingw32-make") or "mingw32-make"
    print(f"llvm-mingw build: cc={cc} cxx={cxx}", flush=True)

    apply_mingw_patch(src)

    tbb_prefix = os.path.abspath("tbb-mingw")
    build_onetbb_shared_mingw(tbb_prefix, cc, cxx, make)

    bld = "openusd-build-mingw"
    config = [
        "cmake", "-S", src, "-B", bld, "-G", "MinGW Makefiles",
        f"-DCMAKE_MAKE_PROGRAM={make}",
        f"-DCMAKE_C_COMPILER={cc}", f"-DCMAKE_CXX_COMPILER={cxx}",
        "-DCMAKE_BUILD_TYPE=" + ("Release" if release else "RelWithDebInfo"),
        f"-DCMAKE_INSTALL_PREFIX={build_dir}",
        f"-DCMAKE_PREFIX_PATH={tbb_prefix}",
        "-DCMAKE_CXX_STANDARD=17", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        "-DPXR_ENABLE_PYTHON_SUPPORT=OFF", "-DPXR_BUILD_MONOLITHIC=ON",
        "-DPXR_BUILD_IMAGING=OFF", "-DPXR_BUILD_USD_IMAGING=OFF",
        "-DPXR_BUILD_USDVIEW=OFF", "-DPXR_BUILD_TESTS=OFF",
        "-DPXR_BUILD_EXAMPLES=OFF", "-DPXR_BUILD_TUTORIALS=OFF",
        "-DPXR_BUILD_DOCUMENTATION=OFF", "-DPXR_ENABLE_MATERIALX_SUPPORT=OFF",
        "-DPXR_ENABLE_VULKAN_SUPPORT=OFF", "-DPXR_ENABLE_OPENVDB_SUPPORT=OFF",
    ] + sccache_args
    _run(config)
    _run(["cmake", "--build", bld, "-j", str(os.cpu_count() or 4), "--target", "usd_m"])

    # USD's install tries to install a few bin tools that don't exist in this
    # headless/no-python build and can fail late; the libs/headers/plugins are
    # already staged by then, so tolerate a non-zero install and verify below.
    subprocess.run(["cmake", "--install", bld], check=False)

    lib = os.path.join(build_dir, "lib", "libusd_ms.dll")
    if not os.path.exists(lib):
        sys.exit(f"windows-gnu build incomplete: {lib} missing")

    # USD's install can skip the top-level plugin-registry aggregator; without it
    # PlugRegistry finds 0 plugins and any file-format op fatal-aborts.
    agg = os.path.join(build_dir, "lib", "usd", "plugInfo.json")
    os.makedirs(os.path.dirname(agg), exist_ok=True)
    with open(agg, "w", encoding="utf-8") as f:
        f.write('{\n    "Includes": [ "*/resources/" ]\n}\n')

    # Ship the shared oneTBB (dll + import lib) and its headers so the archive is
    # self-contained for a mingw consumer (usd_ms dynamically needs libtbb12.dll,
    # and pxr headers include <tbb/...>).
    dst_lib = os.path.join(build_dir, "lib")
    dst_bin = os.path.join(build_dir, "bin")
    os.makedirs(dst_bin, exist_ok=True)
    for rel, dst in [("bin/libtbb12.dll", dst_bin), ("bin/libtbbmalloc.dll", dst_bin),
                     ("lib/libtbb12.dll.a", dst_lib), ("lib/libtbbmalloc.dll.a", dst_lib)]:
        s = os.path.join(tbb_prefix, rel)
        if os.path.exists(s):
            shutil.copy(s, dst)
    # loaders search lib/ too; keep a copy of the tbb runtime beside usd_ms
    for name in ("libtbb12.dll", "libtbbmalloc.dll"):
        s = os.path.join(tbb_prefix, "bin", name)
        if os.path.exists(s):
            shutil.copy(s, dst_lib)
    for hdr in ("tbb", "oneapi"):
        s = os.path.join(tbb_prefix, "include", hdr)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(build_dir, "include", hdr), dirs_exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", required=True, help="OpenUSD install prefix")
    ap.add_argument("--with-python", action="store_true",
                    help="build with Python (only needed to obtain usdGenSchema)")
    ap.add_argument("--release", action="store_true",
                    help="release build variant (else relwithdebuginfo)")
    ap.add_argument("--package", action="store_true",
                    help="after building, tar.gz the build dir to OPENUSD_ARCHIVE")
    args = ap.parse_args()

    version = os.environ.get("OPENUSD_VERSION", "26.05")
    plat = platform_name()
    target = os.environ.get("OPENUSD_TARGET", "")
    src = f"openusd-{version}-src"

    if not os.path.exists(src):
        print(f"Cloning OpenUSD v{version} ...", flush=True)
        rc = subprocess.run([
            "git", "clone", "-b", "v" + version, "--recursive", "--depth", "2",
            "https://github.com/PixarAnimationStudios/OpenUSD.git", src]).returncode
        if rc != 0:
            sys.exit(f"OpenUSD clone failed ({rc})")

    # llvm-mingw (GNU ABI) Windows build: usd_ms consumable by mingw toolchains.
    if target.endswith("windows-gnu"):
        gnu_lib = f"{args.build_dir}/lib/libusd_ms.dll"
        if os.path.exists(gnu_lib):
            print(f"OpenUSD (windows-gnu) already built at {args.build_dir}", flush=True)
        else:
            use_sccache = os.environ.get("USE_SCCACHE", "") not in ("", "0", "no", "false")
            sccache = shutil.which("sccache") if use_sccache else None
            sccache_args = []
            if sccache:
                launcher = sccache.replace("\\", "/")
                sccache_args = [f"-DCMAKE_C_COMPILER_LAUNCHER={launcher}",
                                f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}"]
            build_windows_gnu(src, args.build_dir, args.release, sccache_args)
        if args.package:
            package(args.build_dir)
        return

    patch_vs2026(src)

    lib = {
        "windows": f"{args.build_dir}/lib/usd_ms.dll",
        "macos": f"{args.build_dir}/lib/libusd_ms.dylib",
    }.get(plat, f"{args.build_dir}/lib/libusd_ms.so")
    if os.path.exists(lib):
        print(f"OpenUSD already built at {args.build_dir}", flush=True)
        if args.package:
            package(args.build_dir)
        return

    env = {}
    if plat == "windows":
        env.update(windows_msvc_env())
    else:
        env["PATH"] = os.environ.get("PATH", "")
    for k, v in os.environ.items():
        if k.startswith(("SCCACHE_", "ACTIONS_")) or k == "USE_SCCACHE":
            env[k] = v

    cmake_args = "-DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_CXX_STANDARD=17"
    use_sccache = os.environ.get("USE_SCCACHE", "") not in ("", "0", "no", "false")
    sccache = shutil.which("sccache") if use_sccache else None
    if sccache:
        launcher = sccache.replace("\\", "/")
        cmake_args += (f' -DCMAKE_C_COMPILER_LAUNCHER="{launcher}"'
                       f' -DCMAKE_CXX_COMPILER_LAUNCHER="{launcher}"')
        print(f"sccache enabled: {sccache}", flush=True)

    python = "python3" if shutil.which("python3") else "python"
    cmd = [
        python, f"{src}/build_scripts/build_usd.py", args.build_dir, "--verbose",
        "--build-variant", "release" if args.release else "relwithdebuginfo",
        "--build-monolithic",
        "--python" if args.with_python else "--no-python",
        "--no-examples", "--no-tutorials", "--no-tools", "--no-debug-python",
        "--no-openvdb", "--no-usdview", "--no-imaging", "--no-vulkan",
        "--no-materialx", "--onetbb",
        "--no-compiler-cache" if sccache else "--compiler-cache",
        "--cmake-build-args", cmake_args,
    ]
    if args.with_python:
        cmd += ["--build-python-info", *build_python_info(plat)]

    print("Building OpenUSD ...", flush=True)
    rc = subprocess.run(cmd, env=env).returncode
    if rc != 0:
        sys.exit(f"OpenUSD build failed ({rc})")

    if args.package:
        package(args.build_dir)


# build_usd.py's install prefix also holds the dependency source (`src/`) and
# build-intermediate (`build/`) trees — huge on Windows (MSVC .obj/.pdb). Ship
# only the installed SDK.
_PACKAGE_SKIP = {"src", "build"}


def _deterministic_tarinfo(ti):
    """Normalize metadata so the archive depends only on file contents+layout,
    not on the machine/time it was packaged: zero mtimes, drop owner identity,
    and canonicalize permission bits."""
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    if ti.isdir():
        ti.mode = 0o755
    else:
        ti.mode = 0o755 if (ti.mode & 0o111) else 0o644
    return ti


def _sorted_arcnames(build_dir):
    """Every entry (dirs + files) under build_dir except the skipped trees, as
    forward-slash relative arcnames in a stable, OS-independent sort order."""
    names = []
    for entry in os.listdir(build_dir):
        if entry in _PACKAGE_SKIP:
            continue
        full = os.path.join(build_dir, entry)
        if os.path.isdir(full):
            for root, dirs, files in os.walk(full):
                dirs.sort()
                names.append(os.path.relpath(root, build_dir))
                for f in files:
                    names.append(os.path.relpath(os.path.join(root, f), build_dir))
        else:
            names.append(entry)
    return sorted({n.replace(os.sep, "/") for n in names})


def package(build_dir):
    """Deterministically tar.gz the installed OpenUSD SDK from build_dir into
    OPENUSD_ARCHIVE (so extracting yields include/, lib/, plugin/, bin/, share/,
    cmake/ at the root), excluding the dependency source/build-intermediate
    trees. The archive is byte-reproducible for identical build outputs: entries
    are emitted in a fixed order with normalized metadata and gzip mtime=0, so
    re-packaging the same tree yields the same bytes (and same checksum)."""
    archive = os.environ.get("OPENUSD_ARCHIVE")
    if not archive:
        sys.exit("OPENUSD_ARCHIVE not set; cannot package")
    os.makedirs(os.path.dirname(os.path.abspath(archive)), exist_ok=True)
    print(f"Packaging {build_dir} -> {archive}", flush=True)
    with open(archive, "wb") as f_out:
        # mtime=0 keeps the gzip header itself reproducible.
        with gzip.GzipFile(filename="", fileobj=f_out, mode="wb", mtime=0,
                           compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for arc in _sorted_arcnames(build_dir):
                    src = os.path.join(build_dir, arc.replace("/", os.sep))
                    ti = _deterministic_tarinfo(tar.gettarinfo(src, arcname=arc))
                    if ti.isfile():
                        with open(src, "rb") as fh:
                            tar.addfile(ti, fh)
                    else:
                        tar.addfile(ti)


if __name__ == "__main__":
    main()
