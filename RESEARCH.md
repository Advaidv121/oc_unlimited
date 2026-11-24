# Free-tier reliability investigation — 24 November 2026

## Findings

The proxy is an HTTPS CONNECT relay. WARP changing its public route does not change HTTP credentials, client identity, project, session, or cache affinity inside TLS. A route probe verifies connectivity, not model eligibility or remaining allowance.

The OpenCode 2.4.1 request builder sends session and parent-session IDs, project ID, client identity, User-Agent, and session affinity. Authentication headers are added downstream. Children share their parent's affinity, and forks inherit their source session's affinity. These facts describe client behavior; they do not prove which fields the gateway meters.

Source references:

- [2.4.1 request builder](https://github.com/anomalyco/opencode/blob/v2.4.1/packages/core/src/session/model-request.ts)
- [2.4.1 session affinity](https://github.com/anomalyco/opencode/blob/v2.4.1/packages/core/src/session/affinity.ts)
- [HTTP and retry plugin hooks](https://opencode.ai/docs/build/plugins/server/)

## Evidence and limitations

- Local saved assistant records contain HTTP 429 free-tier rejections with multi-hour retry delays. Several repeated errors count down toward approximately the same reset time. This is consistent with a quota window, but the same error can also express client eligibility restrictions.
- Local failures follow successful requests across a wide range of context sizes. A later successful call in the previously failing session demonstrates that its ID and large context are not unconditionally blocked.
- A passive plugin loaded in Desktop's running 2.4.1 service and observed real requests carrying client=desktop, a 32,000-token output limit, fourteen tools, and hundreds of messages. The observed response was HTTP 200. These parameters are measurements, not established causes of the earlier failure.
- Beam Free and HY4 Flash Free returned the requested marker through the existing service. Nemotron Lightning completed without a saved quota error, but did not satisfy the exact marker check. These are small availability probes, not sustained-load validation.

Official and community evidence:

- [Maintainer free-tier troubleshooting](https://github.com/anomalyco/opencode/issues/59387): requires current v2, distinguishes free-tier access from paid access, and directs account-specific failures to support.
- [Maintainer response about network and old-session restrictions](https://github.com/anomalyco/opencode/issues/60935): allowance is not tied only to a Console account; old/imported sessions and VPN/proxy connections can behave differently.
- [Community report of per-model availability](https://github.com/anomalyco/opencode/issues/64334): another model answered while one was exhausted. This supports testing model fallback but does not establish a universal per-model quota formula.
- [Official model selection](https://opencode.ai/docs/models/): selecting a model applies to the current session; changing the configured default does not override an existing session selection.
- [Proxy upstream issue tracker](https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-/issues/14): reports continued failures; no reproducible universal fix is established by the project's claims.

## Practical recovery

Use the official client and retain its normal request identity. If one model rejects a task, selecting another available free model is a supported way to continue the same session. A short probe does not guarantee that the full conversation will fit or that a task will complete. If all free models reject requests, respect the reported delay and investigate network/client eligibility or account support. The documented reset time is server-provided and is not a guarantee that waiting will resolve eligibility failures.

The plugin leaves the model, request parameters, route, and identifiers intact. After observing a stalled stream followed by three lengthy transport retries and a free-tier rejection, the plugin now permits at most one transport retry. It preserves free-tier rejection decisions. This bounds replay waste; it does not fix quota or eligibility. At the next failure, correlate the response status/retry event with request kind and context metrics. Only then compare normal-network versus proxy behavior, or reduce output/context settings in an isolated test. Changing many variables simultaneously makes a temporary success uninterpretable.

No verified unlimited workaround has been established. The gateway's implementation and quota formula are not established by the client source, public issue comments, or route changes.

## Port of earlier session recovery

The earlier native engine had successful saved recovery evidence, including a real same-tab 94,620-token test on 15 November. That mechanism was absent from the later CONNECT relay. An opt-in plugin port now implements bounded replacement headers, parent/child consistency, in-memory successful mappings, and success/failure cooldowns. Eight focused JavaScript tests passed.

The 24 November same-chat DeepSeek test recorded an initial 429, one retry with a different hashed upstream affinity, then another 429. The replacement was discarded and further automatic attempts stopped. The port leaves OpenCode's separately built promptCacheKey intact and therefore does not reproduce every part of the former engine's serialization. It is disabled by default because this live test did not restore DeepSeek access. Local chat IDs and messages were not rewritten.
