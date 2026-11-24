# macOS setup

This setup routes OpenCode Desktop's built-in models through local WARP. It was tested on Apple silicon with OrbStack. Upstream quotas still apply.

Install and open OpenCode Desktop and OrbStack. The optional guard also requires Apple’s Command Line Tools for Python 3. Run these commands from the checkout. Review the [Cloudflare WARP terms](https://www.cloudflare.com/application/terms/) before starting: the container startup script accepts them when registering.

    docker build -f Dockerfile.mac -t opencode-unlimited-proxy-v4 .
    docker run -d --name opencode-unlimited-proxy-v4 --restart unless-stopped --cap-add NET_ADMIN --device /dev/net/tun -p 127.0.0.1:41988:41987 --mount type=volume,src=oc-unlimited-warp,dst=/var/lib/cloudflare-warp opencode-unlimited-proxy-v4
    curl -fsS --max-time 10 -x http://127.0.0.1:41988 https://cloudflare.com/cdn-cgi/trace | grep '^warp='

Wait for the probe to report `warp=on`, then connect OpenCode. Use the bundled CLI version that matches your installation. For example, with Desktop CLI 2.3.2 on Apple silicon:

    cli="$HOME/Library/Application Support/ai.opencode.desktop/cli/2.3.2-arm64/opencode-cli"
    "$cli" service get
    "$cli" service set env HTTP_PROXY http://127.0.0.1:41988
    "$cli" service set env HTTPS_PROXY http://127.0.0.1:41988
    "$cli" service set env NO_PROXY 127.0.0.1,localhost,::1
    "$cli" service restart

Open OpenCode normally and select a built-in model. Keep OrbStack running. Save any previous proxy settings before changing them so you can restore them later.

For optional automatic WARP rotation, run the following from the checkout:

    bash scripts/install-mac-guard.sh

The guard reads the local error log and checks the desktop service's active-session API before rotating. It attempts at most two rotations per hour and verifies whether the public WARP IPv6 block changed. Retry manually in OpenCode after a rotation. The guard does not resend chat messages or switch accounts.

## Diagnose recurring quota errors

Run this from the checkout to inspect the last seven days of saved failures:

    python3 scripts/quota-diagnostics.py --days 7

The report reads OpenCode's database without modifying it and includes models, status codes, retry delays, and the preceding request's context size. It omits prompts, response bodies, and credentials. A provider.free-limit error with HTTP 429 is an upstream free-tier rejection. OpenCode can use this error for quota exhaustion or client/network eligibility restrictions; the error alone does not distinguish them. The context size of a preceding successful request does not reveal the provider's quota formula or prove why the next request was rejected.

For passive request tracing on OpenCode 2.4.1, copy the plugin directory to the global plugins folder:

    mkdir -p "$HOME/.config/opencode/plugins"
    cp -R plugins/quota-trace "$HOME/.config/opencode/plugins/oc-quota-trace"

OpenCode watches this directory and loads the plugin automatically. Confirm it appears as oc.quota-trace in the bundled CLI's plugin list. Metadata is written to ~/Library/Application Support/OpenCodeProxyQuotaGuard/request-trace.jsonl. The trace records model, request kind, message/text counts, output limits, header names, HTTP status, and retry decisions. It does not read request or response bodies or change model requests, identifiers, or retries. Session correlation uses a random, process-local keyed hash. Logs rotate at approximately 2 MB, retaining one previous file. Remove the installed plugin folder to stop tracing.

This is an HTTPS CONNECT proxy: it forwards encrypted traffic and cannot inspect or rewrite model requests, quota responses, or session IDs. A verified WARP route change establishes connectivity, not recovered model allowance. The guard waits until all sessions are idle; if it cannot act within five minutes, it reports the expired event. For a confirmed free-limit response, use the reported retry delay or choose a model with available allowance. Repeated retries are not a guarantee of recovery.

To stop the guard, run:

    launchctl bootout "gui/$(id -u)/local.opencode-proxy-quota-guard"
    rm "$HOME/Library/LaunchAgents/local.opencode-proxy-quota-guard.plist"

Restore your previous service environment settings and restart OpenCode before stopping the proxy with `docker stop opencode-unlimited-proxy-v4`.

Thanks to [ShadowySphinX (Avatar)](https://github.com/ShadowySphinX) for the upstream project. See [LICENSE](LICENSE).
