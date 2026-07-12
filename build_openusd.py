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
    src = f"openusd-{version}-src"

    if not os.path.exists(src):
        print(f"Cloning OpenUSD v{version} ...", flush=True)
        rc = subprocess.run([
            "git", "clone", "-b", "v" + version, "--recursive", "--depth", "2",
            "https://github.com/PixarAnimationStudios/OpenUSD.git", src]).returncode
        if rc != 0:
            sys.exit(f"OpenUSD clone failed ({rc})")
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


def package(build_dir):
    """tar.gz the installed OpenUSD SDK from build_dir into OPENUSD_ARCHIVE (so
    extracting yields include/, lib/, plugin/, bin/, share/, cmake/ at the root),
    excluding the dependency source/build-intermediate trees."""
    archive = os.environ.get("OPENUSD_ARCHIVE")
    if not archive:
        sys.exit("OPENUSD_ARCHIVE not set; cannot package")
    os.makedirs(os.path.dirname(os.path.abspath(archive)), exist_ok=True)
    print(f"Packaging {build_dir} -> {archive}", flush=True)
    with tarfile.open(archive, "w:gz") as tar:
        for entry in sorted(os.listdir(build_dir)):
            if entry in _PACKAGE_SKIP:
                continue
            tar.add(os.path.join(build_dir, entry), arcname=entry)


if __name__ == "__main__":
    main()
