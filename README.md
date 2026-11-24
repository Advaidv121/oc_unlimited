# oc_unlimited

A local WARP routing setup for OpenCode Desktop on macOS, with an optional background guard that checks for quota errors and verifies route changes. Upstream quotas still apply.

## Set up on macOS

1. Install and open OpenCode Desktop and OrbStack. This setup was tested on Apple silicon. Review the [Cloudflare WARP terms](https://www.cloudflare.com/application/terms/) before starting: the container accepts them when it registers.

2. Clone the repo, build the image, and start the local proxy:

   ```sh
   git clone https://github.com/Advaidv121/oc_unlimited.git
   cd oc_unlimited
   docker build -f Dockerfile.mac -t opencode-unlimited-proxy-v4 .
   docker run -d --name opencode-unlimited-proxy-v4 --restart unless-stopped \
     --cap-add NET_ADMIN --device /dev/net/tun -p 127.0.0.1:41988:41987 \
     --mount type=volume,src=oc-unlimited-warp,dst=/var/lib/cloudflare-warp \
     opencode-unlimited-proxy-v4
   ```

3. Check that WARP is connected. If the container is still starting, wait a few seconds and try again. Continue when this prints `warp=on`:

   ```sh
   curl -fsS --max-time 10 -x http://127.0.0.1:41988 \
     https://cloudflare.com/cdn-cgi/trace | grep '^warp='
   ```

4. Find the bundled OpenCode CLI for your installed Desktop version:

   ```sh
   find "$HOME/Library/Application Support/ai.opencode.desktop/cli" -name opencode-cli -type f
   ```

   Set `cli` to the matching path. The example below is for version 2.3.2 on Apple silicon. Save any existing proxy settings shown by `service get` so you can restore them later.

   ```sh
   cli="$HOME/Library/Application Support/ai.opencode.desktop/cli/2.3.2-arm64/opencode-cli"
   "$cli" service get
   "$cli" service set env HTTP_PROXY http://127.0.0.1:41988
   "$cli" service set env HTTPS_PROXY http://127.0.0.1:41988
   "$cli" service set env NO_PROXY 127.0.0.1,localhost,::1
   "$cli" service restart
   ```

5. Open OpenCode Desktop normally and pick a built-in model. Keep OrbStack running while using the proxy.

See the [macOS guide](MACOS.md) for the optional guard and how to undo the setup, or the [upstream documentation](UPSTREAM.md) for other platforms.

For automatic recovery when a free model rejects a request, install the included OpenCode 2.4.1 plugin from the checkout:

    mkdir -p "$HOME/.config/opencode/plugins/oc-quota-trace"
    cp plugins/quota-trace/*.js plugins/quota-trace/package.json "$HOME/.config/opencode/plugins/oc-quota-trace/"

It retries the same chat step with a compatible available free model, without switching providers or replacing chat IDs. See [automatic free-model fallback](MACOS.md#automatic-free-model-fallback) for its bounds and how to disable it.

Based on the work of [ShadowySphinX (Avatar)](https://github.com/ShadowySphinX). Distributed under the [MIT License](LICENSE).
