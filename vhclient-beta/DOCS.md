# Home Assistant App: VirtualHere Client (Beta)

This is a **beta** / experimental app (`slug: vhclient-beta`, stage: experimental). Behavior, configuration, and options may change without a stable migration path.

## About

Runs the [VirtualHere Linux console client](https://www.virtualhere.com/linux_console) so USB devices shared by a VirtualHere server can be used on the Home Assistant host.

All control uses the [Client API](https://www.virtualhere.com/client_api) over Linux named pipes (`/tmp/vhclient` and `/tmp/vhclient_response`) via the in-app `vhctl` Python tool. This app **never writes** `vhclient.ini`; VirtualHere may still persist its own state there when started with `-c /config/vhclient.ini`.

## How to use

By default the client uses built-in server discovery and auto-uses all devices. Change that with the options below, or enable **No client config** to skip API configuration from the app.

### Option: `discover_interval`

Seconds between automatic USB device list refreshes (nickname / in-use status / new devices). Changes are picked up by the watcher without editing the service command.

### Watchdog

Supervisor can monitor app health when **Watchdog** is enabled for the app. This app exposes:

`http://<host>:8199/health`

### Home Assistant USB discovery

This app enables `hassio_api` (role `default`) for Supervisor options updates, and `homeassistant_api` for Home Assistant Core. After a VirtualHere device is attached or detached (`USE` / `STOP USING`, or Auto-Use All bind), it asks Core to run `usb/scan` so integrations can pick up the new USB hardware.

Optional MQTT (`services: mqtt:want`) is used only when **Home Assistant entities** is enabled. The app reads the broker from the Supervisor MQTT service and publishes discovery, state, and availability on that broker. Switch clicks arrive on the `command_topic` from each discovery message. Discovery is published again when the broker connects and when Home Assistant sends its MQTT birth message (`homeassistant/status` = `online`).

## Configuration

### Option: `ha_entities`

When enabled, the app creates a Home Assistant **device** (`VirtualHere Client`) via MQTT discovery with:

- **control switches**: Auto-Find, Auto-Use All (updates addon options; applied by `vhctl watch`)
- a **switch** per USB device (`use` / stop using); unavailable while another client holds the device
- an **info sensor** per USB device (state = in-use-by; attributes include address, hub, nickname, product, vendor/ids, serial, auto-use, client id, bound host, connection/bound flags, configured `use`, controllable)
- client **diagnostic sensor**: USB Device Count

Requires the Home Assistant **MQTT** integration (Mosquitto addon or another broker). Disable this option to remove the discovered device and entities. If MQTT is missing, the app retries a few times then turns `ha_entities` off automatically (re-enable after MQTT is available).

### Option: `autouse`

Turn auto-use all devices on / off via Client API (`AUTO USE ALL` / `AUTO USE CLEAR ALL`).

### Option: `autofind`

Turn auto-find on / off via Client API (`AUTOFIND` toggle, applied only when current state differs).

### Option: `usbdevices`

List of devices seen on the server. Labels prefer the VirtualHere nickname/name over the USB product string, and always include `(In-use by …)` (including you / NO ONE). Reload/restart to refresh. Set `use` to `true`/`false` per device.

When `autouse` is off, `vhctl sync-devices` applies `USE` / `STOP USING` and toggles `AUTO USE DEVICE` only after `DEVICE INFO`. Changing any USB device `use` flag (configuration UI or HA entity switch) turns **Auto-use all devices** off so per-device control applies.

### Option: `preventconfig`

When enabled, the app does not apply autofind/autouse/manualhub or per-device USE commands via the Client API.

### Option: `manualhub`

Manual hub address (`host:port`) via Client API (`MANUAL HUB ADD` / `REMOVE`).

## Privileges

This app needs host USB, kernel modules (`vhci-hcd`), `SYS_ADMIN`, and host networking for USB/IP. A custom `apparmor.txt` is shipped so AppArmor stays enabled.

If the app fails to start with AppArmor denials, on the HA host run:

```bash
journalctl _TRANSPORT="audit" -g 'apparmor="DENIED"'
```
