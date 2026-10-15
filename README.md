# Netatmo Smoke & CO Alarms for Home Assistant

[![Validate](https://github.com/gmb2012/ha-netatmo-smoke-co/actions/workflows/validate.yml/badge.svg)](https://github.com/gmb2012/ha-netatmo-smoke-co/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

> Runs in the author's home since October 2026 (3 smoke alarms, 1 CO alarm). Smoke alarms are
> tested with real alarms end to end (test spray, 15.10.2026); the CO path is implemented the same way but
> **not tested with real CO**. Feedback via issues is welcome.

Unofficial Home Assistant integration for the **Netatmo Smart Smoke Alarm (NSD)** and the
**Netatmo Smart Carbon Monoxide Alarm (NCO)**. The built-in Netatmo integration does not expose these
devices. This one uses the official Netatmo Connect API: OAuth via Application Credentials, a webhook
through the Home Assistant Cloud (Nabu Casa) as wake-up call, and polling every 5 minutes as fallback.

> **Disclaimer:** Not affiliated with Netatmo / Legrand. This is an **additional** notification
> channel, not a replacement for the alarms' own sirens or the Netatmo app. Netatmo delivers the
> webhook **about 1.5–4 minutes after** the alarm went off (measured), so do not rely on it for
> real-time reaction inside the house.

*Deutsch weiter unten.*

## Requirements

- Home Assistant Cloud (Nabu Casa) subscription for the webhook. Without it the integration only polls
  (alarm visible after up to 5 minutes plus the Netatmo delay).
- A Netatmo developer app of your own (dev.netatmo.com → *Create an app*), redirect URI
  `https://my.home-assistant.io/redirect/oauth`. Use a dedicated app: Netatmo allows one webhook URL
  per app.

## Installation

**HACS (recommended):** HACS → ⋮ → *Custom repositories* → `https://github.com/gmb2012/ha-netatmo-smoke-co`,
category *Integration* → install *Netatmo Smoke & CO Alarms* → restart Home Assistant.

**Manual:** copy `custom_components/netatmo_melder` to `/config/custom_components/` and restart.

## Setup

1. Settings → Devices & services → ⋮ → *Application credentials* → add *Netatmo Smoke & CO Alarms* with
   the client ID and secret of your developer app.
2. Add the integration *Netatmo Smoke & CO Alarms* and log in at Netatmo (scopes
   `read_smokedetector read_carbonmonoxidedetector`).
3. Use the event `netatmo_melder_alarm` in an automation for a critical push (example below).

## Example: critical push

```yaml
triggers:
  - trigger: event
    event_type: netatmo_melder_alarm
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "{{ 'Smoke' if trigger.event.data.type == 'smoke' else 'CO' }} alarm: {{ trigger.event.data.name }}"
      message: "{{ 'Still active' if trigger.event.data.active else 'Already over' }}"
      data:
        ttl: 0
        priority: high
        channel: alarm_stream   # Android: loud, also in Do Not Disturb
mode: queued
```

## Real-time alternative: HomeKit over Bluetooth

The alarms also speak HomeKit over Bluetooth LE. With an ESPHome Bluetooth proxy close to an alarm,
Home Assistant's built-in **HomeKit Controller** integration can pair it locally and report smoke within
seconds. Both paths can run in parallel; this integration then serves as the fallback that covers all
alarms and reports battery/chamber events.

## How it works

- The webhook body is only a **wake-up call**: its HMAC signature (`X-Netatmo-secret`) is checked, then
  the events are fetched via the authenticated API. State is never taken from the webhook body. The
  handler always answers 200 (Netatmo blocks a webhook for 24 h after 5 failures).
- `smoke` with `sub_type` 1 = smoke detected, 0 = smoke cleared; `hush` mutes for 15 min without ending
  the alarm. `co_detected` is assumed to work the same way (**not tested** with real CO).
- Each new alarm fires the event `netatmo_melder_alarm` (`module_id`, `name`, `type`, `event_id`,
  `time`, `active` = alarm still on when seen), however late it is seen. Only on the first refresh after setup or a restart, alarms older
  than 10 minutes set the state without the event.
- The alarms sleep and only connect on events, so *last seen* changes rarely; a detector counts as
  offline after 45 days without contact. Battery state is only known from `battery_status` events.
- A Home Assistant Cloud subscription added later requires reloading the integration to enable the
  webhook.
- Acknowledging while smoke is still present keeps the sensor off until Netatmo sends a new `smoke`
  event.

## Entities (per detector)

| Entity | Description |
|---|---|
| Binary sensor *Smoke* (NSD) / *Carbon monoxide* (NCO) | Alarm active |
| Binary sensor *Tamper*, *Battery*, *Detection chamber* (NSD), *Connectivity* | Status from events |
| Sensor *Last event*, *Wi-Fi strength*, *Last seen* | Diagnostics |
| Button *Acknowledge alarm* | Resets the alarm state locally |

Plus one sensor *Webhook last received* for the integration.

## Development

```bash
pip install -r requirements_test.txt
pytest -q tests --cov=custom_components.netatmo_melder   # Python 3.13
ruff check .
```

## Deutsch

Inoffizielle Integration für die Netatmo-Rauchmelder (NSD) und den CO-Melder (NCO) über die offizielle
Netatmo-API (OAuth, Webhook via Nabu Casa, Abfrage alle 5 min). **Zusatzkanal**, ersetzt weder die
Sirenen noch die Netatmo-App; der Webhook kommt **etwa 1,5–4 Minuten** nach dem Alarm.

- **Installation:** HACS → *Benutzerdefinierte Repositories* → `https://github.com/gmb2012/ha-netatmo-smoke-co`
  (Kategorie *Integration*) → installieren → HA neu starten.
- **Einrichtung:** eigene Netatmo-Dev-App (Redirect `https://my.home-assistant.io/redirect/oauth`) →
  in HA unter *Anmeldeinformationen für Anwendungen* Client-ID und -Secret eintragen → Integration
  hinzufügen und bei Netatmo anmelden.
- **Alarm:** Event `netatmo_melder_alarm` für jeden neuen Alarm (nur direkt nach einem Neustart werden Alarme > 10 min ohne Event übernommen); damit z. B. einen kritischen Push
  auslösen. CO-Alarm ist analog zum Rauch umgesetzt, aber nicht mit echtem CO getestet.
- **Offline:** nach 45 Tagen ohne Kontakt; Batterie nur über `battery_status`-Ereignisse bekannt.
