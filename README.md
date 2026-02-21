## ⚠️ # MyHOME – Stability Mod (Unofficial Fork)

This repository is a personal fork of:
**anotherjulien/MyHOME**

The goal of this fork is to improve runtime stability and reduce gateway-side stress caused by aggressive polling and fragile reconnect handling.

This is an experimental branch intended for testing and learning purposes.
It is **not officially supported**, not reviewed, and not recommended for production environments.

---

### 🔧 Stability & Behavior Changes

**2026-02 – Initial Stability Pass**

- Added watchdog logic to the event listener loop to prevent infinite awaits on half-open sockets.
- Improved reconnect handling with exponential backoff.
- Added clean shutdown handling for async tasks.
- Clarified internal task lifecycle (event session vs command session).
- Reduced aggressive polling intervals:
  - `sensor.py` SCAN_INTERVAL from 60s → 300s
  - `binary_sensor.py` SCAN_INTERVAL from 5s → 30s
- Added inline documentation to clarify async flow and session management.

These changes aim to:
- Reduce frequent TCP reconnect churn.
- Minimize command session resets.
- Improve long-running stability of the integration.

---

### 📌 Original Project

Original upstream repository:
https://github.com/anotherjulien/MyHOME

All credits for the original implementation go to the upstream author.
This fork only focuses on experimental stability improvements.

# What's up?

I'm afraid it's time to be blunt, I cannot maintain this integration any longer, not in any meaningful way at least.

I'm open for someone to take over this and OWNd's repositories.  
I'd strongly prefer someone who has extensive experience with a proper development workflow, since I feel that's something that has been missing from this project.  
I'd love for this to become a core integration one day but I have no idea how much work would be needed to achieve that.

Anyway, If you think you can take over code ownership for this, let me know.

# MyHOME
MyHOME integration for Home-Assistant

## Installation
The integration is able to install the gateway via the Home-Assistant graphical user interface, configuring the different devices needs to be done in YAML files however.

Some common gateways should be auto-discovered, but it is still possible to force the inclusion of a gateway not discovered. One limitation however is that the gateway needs to be in the same network as your Home-Assistant instance.

It is possible that upon first install (and updates), the OWNd listener process crashes and you do not get any status feedback on your devices. If such is the case, a restart of Home Assistant should solve the issue.

## BEWARE

If you've been using this integration in version 0.8 and prior, configuration structure has changed and you need to create and populate the appropriate config file. See below for instructions.


## Configuration and use

Please find the [configuration](https://github.com/anotherjulien/MyHOME/wiki/Configuration) on the project's wiki!  
[Advanced uses](https://github.com/anotherjulien/MyHOME/wiki/Advanced-uses) are also listed in the wiki.
