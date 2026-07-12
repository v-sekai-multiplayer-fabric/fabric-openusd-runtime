defmodule StageRuntime do
  @moduledoc """
  Prebuilt, per-triplet OpenUSD runtime (monolithic `usd_ms` + headers + plugins).

  Modeled on `elixir-nx/xla`: by default `mix compile` **downloads** the prebuilt
  archive matching the host triplet from this repo's GitHub releases, verifies it
  against `checksum.txt`, and unpacks it. Set `OPENUSD_BUILD=true` to build
  OpenUSD from source instead (via the `Makefile` + `build_openusd.py`).

  Downstream NIFs consume `StageRuntime.include_dir/0` and `lib_dir/0`.
  """

  require Logger

  @openusd_version "26.05"
  @github_repo "v-sekai-multiplayer-fabric/fabric-stage-runtime"

  # The GitHub release tag is derived from the package version (single source of
  # truth in the VERSION file), so a published version only ever downloads the
  # archives built and attached for its own tag — build and checksums can never
  # desync across versions. The release workflow builds the archives, generates
  # checksum.txt from those exact files, attaches them to the "v#{version}"
  # release, and publishes to Hex in the same job.
  @version_path Path.join(__DIR__, "../VERSION")
  @external_resource @version_path
  @version File.read!(@version_path) |> String.trim()
  @release_tag "v#{@version}"

  # Two Windows triplets differ only by C++ ABI: -msvc for MSVC consumers (the
  # Godot/Unity adapters), -gnu for llvm-mingw consumers (cloth-fit's NIF). The
  # host defaults to -msvc; select -gnu via OPENUSD_TARGET.
  @targets ~w(x86_64-linux-gnu aarch64-apple-darwin x86_64-windows-msvc x86_64-windows-gnu)

  # Bake the per-triplet checksums in at compile time (populated by CI after the
  # release build). `checksum.txt` lines are "sha256:<hex>  <archive-name>".
  @checksum_path Path.join(__DIR__, "../checksum.txt")
  @external_resource @checksum_path
  @checksums (case File.read(@checksum_path) do
                {:ok, content} ->
                  content
                  |> String.split("\n", trim: true)
                  |> Map.new(fn line ->
                    [sum, name] = String.split(line, "  ", parts: 2)
                    {name, sum}
                  end)

                _ ->
                  %{}
              end)

  @doc "The OpenUSD version this package ships (e.g. \"26.05\")."
  def openusd_version, do: @openusd_version

  @doc "Whether to build OpenUSD from source (OPENUSD_BUILD) rather than download."
  def build?, do: System.get_env("OPENUSD_BUILD") in ~w(1 true)

  @doc "The resolved target triplet for the host (override with OPENUSD_TARGET)."
  def target do
    case System.get_env("OPENUSD_TARGET") do
      nil -> detect_target()
      t -> t
    end
  end

  defp detect_target do
    case :os.type() do
      {:win32, _} ->
        "x86_64-windows-msvc"

      {:unix, osname} ->
        arch =
          :erlang.system_info(:system_architecture)
          |> List.to_string()
          |> String.split("-")
          |> List.first()

        arch = if arch in ~w(arm aarch64), do: "aarch64", else: "x86_64"

        case osname do
          :darwin -> "#{arch}-apple-darwin"
          _ -> "#{arch}-linux-gnu"
        end
    end
  end

  @doc "Basename of the prebuilt archive for `target/0`."
  def archive_filename(target \\ target()) do
    "openusd-#{@openusd_version}-#{target}.tar.gz"
  end

  @doc "Root of the unpacked OpenUSD tree (downloading/building on first use)."
  def root do
    dir = Path.join(cache_dir(), "openusd-#{@openusd_version}-#{target()}")

    unless usd_present?(dir) do
      ensure!(dir)
    end

    dir
  end

  def include_dir, do: Path.join(root(), "include")
  def lib_dir, do: Path.join(root(), "lib")

  @doc "Environment passed to the Makefile by elixir_make."
  def make_env do
    %{
      "PYTHON" => System.find_executable("python3") || System.find_executable("python") || "python",
      "OPENUSD_VERSION" => @openusd_version,
      "OPENUSD_TARGET" => target(),
      "OPENUSD_BUILD_DIR" => Path.join(cache_dir(), "openusd-#{@openusd_version}-#{target()}"),
      "OPENUSD_ARCHIVE" => Path.join(cache_dir(), archive_filename())
    }
  end

  # --- resolution ------------------------------------------------------------

  defp ensure!(dest_dir) do
    archive = resolve_archive!()
    File.mkdir_p!(dest_dir)
    Logger.info("Unpacking #{archive} -> #{dest_dir}")
    :ok = :erl_tar.extract(String.to_charlist(archive), [:compressed, {:cwd, String.to_charlist(dest_dir)}])
  end

  # Returns the path to a verified local archive, downloading it if needed.
  defp resolve_archive! do
    cond do
      path = System.get_env("OPENUSD_ARCHIVE_PATH") ->
        path

      true ->
        cached = Path.join(cache_dir(), archive_filename())

        unless File.exists?(cached) do
          url = System.get_env("OPENUSD_ARCHIVE_URL") || release_url()
          download!(url, cached)
        end

        verify_checksum!(cached)
        cached
    end
  end

  defp release_url do
    "https://github.com/#{@github_repo}/releases/download/#{@release_tag}/#{archive_filename()}"
  end

  defp download!(url, dest) do
    Logger.info("Downloading #{url}")
    File.mkdir_p!(Path.dirname(dest))

    {:ok, _} = Application.ensure_all_started(:inets)
    {:ok, _} = Application.ensure_all_started(:ssl)

    http_opts = [
      ssl: [
        verify: :verify_peer,
        cacertfile: CAStore.file_path() |> String.to_charlist(),
        depth: 3,
        customize_hostname_check: [
          match_fun: :public_key.pkix_verify_hostname_match_fun(:https)
        ]
      ]
    ]

    case :httpc.request(:get, {String.to_charlist(url), []}, http_opts, body_format: :binary) do
      {:ok, {{_, 200, _}, _headers, body}} ->
        File.write!(dest, body)

      {:ok, {{_, status, _}, _, _}} ->
        raise "download failed for #{url}: HTTP #{status}"

      {:error, reason} ->
        raise "download failed for #{url}: #{inspect(reason)}"
    end
  end

  # --- checksums -------------------------------------------------------------

  defp verify_checksum!(archive) do
    name = Path.basename(archive)

    case Map.fetch(checksums(), name) do
      {:ok, expected} ->
        actual = "sha256:" <> (File.read!(archive) |> then(&:crypto.hash(:sha256, &1)) |> Base.encode16(case: :lower))

        if actual != expected do
          File.rm(archive)
          raise "checksum mismatch for #{name}\n  expected: #{expected}\n  actual:   #{actual}"
        end

        :ok

      :error ->
        raise "no checksum recorded for #{name} in checksum.txt"
    end
  end

  @doc false
  def checksums, do: @checksums

  @doc "All triplets this package publishes."
  def targets, do: @targets

  # --- helpers ---------------------------------------------------------------

  defp usd_present?(dir) do
    File.dir?(dir) and Path.wildcard(Path.join([dir, "lib", "*usd_ms*"])) != []
  end

  defp cache_dir do
    base =
      System.get_env("OPENUSD_CACHE_DIR") ||
        Path.join(:filename.basedir(:user_cache, "stage_runtime"), @openusd_version)

    File.mkdir_p!(base)
    base
  end
end
