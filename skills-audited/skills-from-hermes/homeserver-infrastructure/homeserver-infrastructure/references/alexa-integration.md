# Alexa / Amazon Smart Home Integration for Hermes

Options for connecting Hermes to cloud-only smart home platforms (Amazon/Alexa device control, including SmartLife/Tuya blinds/etc) when running on the user's local media server. None have CLI tools — all require a bridge script or container setup.

## Decision Matrix

| Approach | Best For | Complexity | Maintenance Risk | Notes |
|----------|----------|------------|------------------|-------|
| **① Home Assistant (Docker)** | Best overall fit for personal servers that already run Docker | Low-Medium | Medium — HA is mature, but Alexa Media Player community add-on depends on Amazon's undocumented/internal APIs. May break when Amazon updates their web app/frontend. Community-driven fixes exist but are slow to arrive from the maintainers. | User spins up HA in a container, installs [Alexa Media Player](https://github.com/custom-cards/alexa_media_player) integration — syncs all Amazon/Echo devices including SmartLife blinds/etc. Then I hit local HA REST API (`curl` or Python requests) to control them: `POST /api/services/*/call`. No need to go through cloud. |
| **② MQTT Bridge (Bridge Script + Mosquitto)** | Low-resource alternative if user doesn't want full Home Assistant stack | Medium-High — requires bridge script that syncs devices from Amazon API, plus persistent MQTT broker (Mosquitto container) running locally for me to talk to via `mosquitto_pub`. "Open blinds" → I send an publish `"open"` to `home/blinds/living_room` | **Very High** — Amazon's internal APIs are completely undocumented and change constantly. Every AWS or Amazon API update breaks bridge scripts. You need to keep the OAuth refresh tokens fresh (they expire on long-term), handle token rotation etc, some solutions are unmaintained. This is the most fraught path for longevity but can be lightweight if you don't want full HA stack. | Examples include generic `alexa-home-mqtt-script`, custom bridge scripts that sync against Amazon's API using OAuth2 tokens and push state to a local MQTT broker (Mosquitto), so I can talk via `mosquitto_pub` commands locally. Works but maintenance burden is high. |
| **③ Tuya/SwitchBot native protocol** | Direct-communication integrations only if specific devices are involved such as Tuya or SwitchBrands etc) natively with dedicated tooling like [SmartLife/TuyaLocalBridge](https://github.com/SmartThingsCommunity/smartapp-localtuya) (for SmartLife specifically). Bridges these into HA, MQTT, or custom API endpoints. | Low-Medium if you're strictly on those brands + willing to use Tuya's official SDK; can be lower complexity | Medium — These have more predictable APIs than Alexa because they're IoT-specific protocols with dedicated local APIs unlike Amazon's web-driven one-off service models. | Note: some devices (like certain Tuya/SmartLife ones) support a direct local protocol. The [Tuya Local](https://github.com/codalabs/tuya-mqtt/wiki) project connects SmartLife via the Tuya local network API, and Home Assistant has an official [LocalTuya](https://github.com/colinodell/Home-Assistant-Local-Tuya-Integration). This avoids the cloud-dependency entirely but hardware compatibility varies. |
| **④ Amazon Alexa Developer Console / "Official" API Route** ← The "official" route, requires a registered AWS account + Active Dev Account → create a custom Alexa Skill (Smart Home type), authenticate with OAuth2 Client Credentials Grant & manage token refresh logic. I'd install `alexa-cli` wrapper to call `enable/disable/off/on-setbrightness` etc from my Docker. | Long-term most officially supported by Amazon since it's their official API route instead of reverse-engineered internal ones that break when they update/change APIs. However full complexity is higher and you need to maintain OAuth token refresh logic. And a full Alexa Skill setup. | High — requires an official Amazon developer account/skills console registration, complex OAuth flow + cloud hosting | Low — It's the official route so it won't get broken by AWS platform updates (though they do add cost considerations around Lambda usage which can scale up rapidly during high load times). | Requires signing up for **Amazon Developer Console**, creating your own custom Alexa Skill via their API. Much more involved setup initially, but long-term most stable because you're using documented APIs rather than internal/hacky integration techniques that break when Amazon releases a new firmware/API version changes. |

## Recommendations (User Context Specific)

For this user specifically: **Home Assistant approach is optimal.** They already have a powerful media server, are Docker-savvy, and want things that just work until ready to ship it later. HA provides the cleanest path with an exposed REST API I can hit directly via local `curl` and Python scripts — no MQTT needed unless they prefer modular architecture.

### Quick Start for User: Home Assistant Approach

```bash
# On media server, run as Docker container or add to docker-compose.yml
-v /opt/homeassistant:/config \
-p 8123:8123 \
ghcr.io/home-assistant-home-assistant:stable

# Once running: install [Alexa Media Player](https://github.com/custom-cards/alexa_media_player) integration
# Generate Long Lived Access token in HA settings > Users

Then I can control via the user's Amazon Echo devices (including SmartLife blinds etc):
GET  http://localhost:8123/api/states
POST http://localhost:8123/api/services/*/call
```

## Pitfall: "Docker not found" From My Environment

If I run `docker ps` from inside my agent session and get `'docker: command not found'`, that's normal — Docker daemon may not be accessible in the sandbox container even if host has it configured well. Tell user to check on their machine instead of concluding their system doesn't have any containers running). 
