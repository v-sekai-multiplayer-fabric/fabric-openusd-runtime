# fabric-stage-runtime

Special thanks to the origin of this project: https://github.com/Immersive-Data-Center-Management/idtx-flow.

Prebuilt, per-triplet **OpenUSD runtime** (v26.05, monolithic `usd_ms` + headers +
plugin registry) shipped as an Elixir/Hex package — so the `fabric-flow-openusd`
adapters (Godot / Unity / Blender) and Elixir consumers (e.g. cloth-fit) **fetch**
USD instead of recompiling it (~40+ min from source).

Modeled on [`elixir-nx/xla`](https://github.com/elixir-nx/xla): `mix compile`
downloads the archive matching the host triplet from this repo's GitHub releases,
verifies it against `checksum.txt`, and unpacks it. Elixir-native — `elixir_make`
+ a small Python build driver (`build_openusd.py`), no SCons.

## Usage

```elixir
def deps do
  [{:stage_runtime, "~> 26.5"}]
end
```

```elixir
StageRuntime.include_dir()  #=> ".../openusd-26.05-<triplet>/include"
StageRuntime.lib_dir()      #=> ".../openusd-26.05-<triplet>/lib"  (contains usd_ms)
StageRuntime.target()       #=> "x86_64-linux-gnu" | "aarch64-apple-darwin" | "x86_64-windows-msvc"
```

## Triplets

`x86_64-linux-gnu`, `aarch64-apple-darwin`, `x86_64-windows-msvc`.

## Build from source

Set `OPENUSD_BUILD=true` before `mix compile` to build OpenUSD locally instead of
downloading (requires Python 3, CMake, and a C++ toolchain — MSVC on Windows). The
build reuses the flags proven in `fabric-flow-openusd` (`--build-monolithic
--onetbb --no-python`, VS2026 generator patch).

## Env

| var | meaning |
|---|---|
| `OPENUSD_BUILD` | build from source instead of downloading |
| `OPENUSD_TARGET` | override the detected triplet |
| `OPENUSD_CACHE_DIR` | where archives + unpacked trees live |
| `OPENUSD_ARCHIVE_PATH` / `OPENUSD_ARCHIVE_URL` | use a local / custom archive |

## Releases

CI builds each triplet natively and, on a `v26.05.*` tag, attaches the three
`openusd-26.05-<triplet>.tar.gz` archives + `checksum.txt` to the GitHub release.
The Hex package ships only the resolver + build machinery + checksums.
