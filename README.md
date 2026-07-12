# fabric-stage-runtime

Special thanks to the origin of this project: https://github.com/Immersive-Data-Center-Management/idtx-flow.

An OpenUSD runtime shipped as an Elixir/Hex package for `fabric-flow-openusd`
adapters (Godot / Unity / Blender) and Elixir consumers.

## Usage

```elixir
def deps do
  [{:stage_runtime, "~> 0.1.0-dev"}]
end
```

```elixir
StageRuntime.include_dir()  #=> ".../openusd-26.05-<triplet>/include"
StageRuntime.lib_dir()      #=> ".../openusd-26.05-<triplet>/lib"  (contains usd_ms)
StageRuntime.target()       #=> "x86_64-linux-gnu" | "aarch64-apple-darwin" | "x86_64-windows-msvc"
```

## Build from source

Set `OPENUSD_BUILD=true` before `mix compile` to build OpenUSD locally instead of
downloading (requires Python 3, CMake, and a C++ toolchain — MSVC on Windows). The
build reuses the flags in `fabric-flow-openusd`.
