class BidnamicOs < Formula
  desc "Bidnamic OS launcher — connect to the Bidnamic agent in the cloud"
  homepage "https://github.com/bidnamic/homebrew-tap"
  # url/sha256/version are rewritten by .github/workflows/release.yml on
  # every push to main that touches launcher/**.
  url "https://github.com/bidnamic/homebrew-tap/archive/refs/tags/v2026.09.10-df49c06.tar.gz"
  sha256 "cf44ae85e2565ba7f24b0c72b1094b148fa300f5ff83c044ce75fea8c525f04f"
  version "2026.09.10-df49c06"

  depends_on "python@3.14"
  depends_on "awscli"
  depends_on "aws/aws/amazon-efs-utils"
  # session-manager-plugin is a cask. Homebrew formulae can't depend on
  # casks, and post_install cannot safely invoke brew (process lock).
  # `bidnamic-os post-install` installs it via `brew install --cask`
  # after the formula install completes.

  def install
    # Brew-managed venv with boto3. amazon-efs-utils maintains its own
    # private libexec venv for mount.efs — that one is unrelated.
    venv = libexec/"venv"
    system Formula["python@3.14"].opt_bin/"python3.14", "-m", "venv", venv
    system venv/"bin/pip", "install", "--no-cache-dir", "--upgrade", "boto3"

    bin.install "launcher/bidnamic_os.py" => "bidnamic-os"
    inreplace bin/"bidnamic-os" do |s|
      # Shebang → brew-managed venv interpreter.
      s.gsub! "#!/usr/local/share/bidnamic-os/venv/bin/python",
              "#!#{venv}/bin/python"
      # SHARE_DIR → brew pkgshare (TUTORIAL.html derives from this).
      s.gsub!(/^SHARE_DIR = .*/, %Q(SHARE_DIR = Path("#{pkgshare}")))
      # __version__ → CalVer tag from the release workflow.
      s.gsub!(/^__version__ = .*/, %Q(__version__ = "#{version}"))
    end

    pkgshare.install "launcher/TUTORIAL.html"
  end

  def caveats
    <<~EOS
      One-time setup (will prompt for your macOS password):

        bidnamic-os post-install

      Prerequisites you must install yourself first:
        - Tailscale Mac app:  https://tailscale.com/download/mac

      To upgrade (re-runs post-install, so prompts for your macOS password):
        bidnamic-os upgrade

      To remove everything (privileged artefacts and the brew package):
        bidnamic-os uninstall
    EOS
  end

  test do
    assert_equal version.to_s, shell_output("#{bin}/bidnamic-os version").strip
  end
end
