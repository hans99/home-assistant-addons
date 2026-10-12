# Changelog

## 0.7.0

- Remove unused s6 oneshots superseded by `vhctl watch`
- Declare optional MQTT service (`mqtt:want`) for HA entities
- Stop calling deprecated Supervisor `hardware/trigger`; keep Core `usb/scan`
- Tighten watchdog health to pipe-only IPC
- Tighten AppArmor network rules (path-based file list reverted in 0.6.12)
- Disable Auto-Use All when USB device `use` is changed in config or HA switches
- Set `hassio_role: default` for Supervisor API access (with existing `hassio_api`)
- Align Dockerfile with HA apps guidance (pinned `BUILD_FROM`, aarch64/amd64 only, OCI labels)
- Serialize VirtualHere Client API pipe IPC across health / watch / MQTT threads
- Serialize Supervisor options updates to avoid lost `usbdevices` / switch writes
- Do not clear Auto-Use All when a per-USB HA switch is turned on
- HA control switches for Auto-Find and Auto-Use All
- Limit supported architectures to aarch64 and amd64 (current Supervisor arches)
- HA entity sensors: per-USB info (all Client API fields as attributes) plus device count
- Auto-disable `ha_entities` after repeated MQTT failures when the MQTT integration is unavailable
- Make USB use switches unavailable while another client holds the device
- Add Home Assistant Supervisor watchdog via `http://[HOST]:8199/health` (VirtualHere IPC check)
- Make USB device list refresh interval configurable (`discover_interval`, default 30s)
- Enable `homeassistant_api` and trigger Core `usb/scan` (plus best-effort Supervisor `hardware/trigger`) after VirtualHere USE / STOP or Auto-Use attach
- Optional `ha_entities`: MQTT discovery device “VirtualHere Client” with a use/not-use switch per USB device
- Watch addon options and apply USB use / client settings without needing a manual restart
- Read live options from the Supervisor API so sync sees saved `use` flags correctly
- Periodically refresh the USB devices list (labels / new devices)
- Prefer VirtualHere nickname/name over USB product in the USB devices list
- Always show `(In-use by …)` including when in use by you / NO ONE
- Build discovery labels from `GET CLIENT STATE` when available
- Control VirtualHere only via Client API named pipes (`/tmp/vhclient`); never write `vhclient.ini`
- Add Python `vhctl` controller (wait / configure / discover / sync-devices)
- Require `DEVICE INFO` before `USE` / `AUTO USE DEVICE`

## 0.5.0

- Align with current Home Assistant [apps](https://developers.home-assistant.io/docs/apps/) guidance: remove obsolete `build.yaml`, pin base image, add OCI/Hass labels, drop unused `homeassistant_api`
- Fix repository URLs to the Gitea remote
- Improve VirtualHere console control: poll `GET CLIENT STATE` instead of a fixed sleep; use `USE`/`STOP USING` plus `AUTO USE DEVICE` via `-t`
- Remove unused `config-client` s6 service (script was missing)
- Normalize s6 script shebangs to `/command/with-contenv`
- Add custom `apparmor.txt` (AppArmor stays enabled)

## 0.4.2

- Maintenance release

## 0.4.0

- Downloading vhclient

## 0.3.1

- Increased start delay for client to connect to server

## 0.3.0

- Update handling of setting device to used and not used

## 0.2.1

- Added documentation

## 0.2.0

- Updates

## 0.1.0

- Initial release
