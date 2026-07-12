defmodule StageRuntime.MixProject do
  use Mix.Project

  # Package version follows the dev→beta→rc→release tag progression; the OpenUSD
  # version it ships is separate (StageRuntime.openusd_version/0 → "26.05").
  @version "0.1.0-dev.1"
  @source_url "https://github.com/v-sekai-multiplayer-fabric/fabric-stage-runtime"

  def project do
    [
      app: :stage_runtime,
      version: @version,
      elixir: "~> 1.15",
      deps: deps(),
      # Build OpenUSD from source only when OPENUSD_BUILD is set; otherwise
      # `mix compile` downloads the matching prebuilt archive (xla-style).
      compilers: Mix.compilers() ++ if(build?(), do: [:elixir_make], else: []),
      make_env: &StageRuntime.make_env/0,
      make_executable: make_executable(),
      description:
        "Prebuilt, per-triplet USD scene-description runtime (OpenUSD v26.05, monolithic usd_ms) shipped as an Elixir/Hex package.",
      package: package(),
      source_url: @source_url,
      docs: [main: "StageRuntime"]
    ]
  end

  def application, do: [extra_applications: [:logger, :inets, :ssl, :crypto]]

  defp deps do
    [
      {:elixir_make, "~> 0.8", runtime: false},
      {:castore, "~> 1.0"},
      {:ex_doc, "~> 0.34", only: :dev, runtime: false}
    ]
  end

  defp build?, do: System.get_env("OPENUSD_BUILD") in ~w(1 true)

  # The Makefile is trivial GNU/nmake-compatible syntax; on Windows use the
  # default nmake, elsewhere the default make.
  defp make_executable, do: :default

  defp package do
    [
      licenses: ["MIT"],
      links: %{"GitHub" => @source_url, "OpenUSD" => "https://openusd.org"},
      # Ship the build machinery + the resolver + checksums; the large binaries
      # live on the GitHub release, not in the Hex tarball.
      files: ~w(lib Makefile build_openusd.py patches checksum.txt mix.exs README.md LICENSE)
    ]
  end
end
