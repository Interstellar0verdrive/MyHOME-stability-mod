"""Code to handle a MyHome Gateway."""
import asyncio
from typing import Dict, List

from homeassistant.const import (
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_PASSWORD,
    CONF_NAME,
    CONF_MAC,
    CONF_FRIENDLY_NAME,
)
from homeassistant.components.light import DOMAIN as LIGHT
from homeassistant.components.switch import (
    SwitchDeviceClass,
    DOMAIN as SWITCH,
)
from homeassistant.components.button import DOMAIN as BUTTON
from homeassistant.components.cover import DOMAIN as COVER
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    DOMAIN as BINARY_SENSOR,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    DOMAIN as SENSOR,
)
from homeassistant.components.climate import DOMAIN as CLIMATE

from OWNd.connection import OWNSession, OWNEventSession, OWNCommandSession, OWNGateway
from OWNd.message import (
    OWNMessage,
    OWNLightingEvent,
    OWNLightingCommand,
    OWNEnergyEvent,
    OWNAutomationEvent,
    OWNDryContactEvent,
    OWNAuxEvent,
    OWNHeatingEvent,
    OWNHeatingCommand,
    OWNCENPlusEvent,
    OWNCENEvent,
    OWNGatewayEvent,
    OWNGatewayCommand,
    OWNCommand,
)

from .const import (
    CONF_PLATFORMS,
    CONF_FIRMWARE,
    CONF_SSDP_LOCATION,
    CONF_SSDP_ST,
    CONF_DEVICE_TYPE,
    CONF_MANUFACTURER,
    CONF_MANUFACTURER_URL,
    CONF_UDN,
    CONF_SHORT_PRESS,
    CONF_SHORT_RELEASE,
    CONF_LONG_PRESS,
    CONF_LONG_RELEASE,
    DOMAIN,
    LOGGER,
)
from .myhome_device import MyHOMEEntity

from .button import (
    DisableCommandButtonEntity,
    EnableCommandButtonEntity,
)


LOGGER.warning("MyHOME fork Interstellar0verdrive - build 2026-02-20-02")

# ============================================================
# ARCHITECTURE OVERVIEW
# ============================================================
# MyHOMEGatewayHandler maintains TWO independent async channels:
#
# 1) Event Session (OWNEventSession)
#    - Long-lived connection to receive asynchronous events.
#    - Feeds Home Assistant with physical state changes.
#    - Runs inside listening_loop().
#
# 2) Command Session (OWNCommandSession)
#    - Separate long-lived connection for outgoing commands.
#    - Consumes tasks from send_buffer queue.
#    - Runs inside one or more sending_loop() workers.
#
# Both loops implement:
# - Timeout protection (avoid half-open TCP hangs)
# - Automatic reconnect with exponential backoff
# - Minimal watchdog logging for diagnosis
#
# If states stop syncing → suspect Event Session.
# If commands stop working → suspect Command Session.
# ============================================================

class MyHOMEGatewayHandler:
    """Manages a single MyHOME Gateway."""

    def __init__(self, hass, config_entry, generate_events=False):
        build_info = {
            "address": config_entry.data[CONF_HOST],
            "port": config_entry.data[CONF_PORT],
            "password": config_entry.data[CONF_PASSWORD],
            "ssdp_location": config_entry.data[CONF_SSDP_LOCATION],
            "ssdp_st": config_entry.data[CONF_SSDP_ST],
            "deviceType": config_entry.data[CONF_DEVICE_TYPE],
            "friendlyName": config_entry.data[CONF_FRIENDLY_NAME],
            "manufacturer": config_entry.data[CONF_MANUFACTURER],
            "manufacturerURL": config_entry.data[CONF_MANUFACTURER_URL],
            "modelName": config_entry.data[CONF_NAME],
            "modelNumber": config_entry.data[CONF_FIRMWARE],
            "serialNumber": config_entry.data[CONF_MAC],
            "UDN": config_entry.data[CONF_UDN],
        }
        self.hass = hass
        self.config_entry = config_entry
        self.generate_events = generate_events
        self.gateway = OWNGateway(build_info)
        self._stop_event_listener = False
        self._stop_command_workers = False
        # Reflects ONLY the state of the Event Session (listening_loop).
        # It does NOT represent the command session state.
        self.is_connected = False
        self.listening_worker: asyncio.tasks.Task = None
        self.sending_workers: List[asyncio.tasks.Task] = []
        self.send_buffer = asyncio.Queue()

        # Command session watchdog timestamps (monotonic time).
        # We use asyncio.get_running_loop().time() instead of time.time()
        # because it is monotonic and not affected by system clock changes
        # (e.g. NTP adjustments), which is critical for timeout logic.
        self._last_command_sent_monotonic = None
        self._last_command_response_monotonic = None

    @property
    def mac(self) -> str:
        return self.gateway.serial

    @property
    def unique_id(self) -> str:
        return self.mac

    @property
    def log_id(self) -> str:
        return self.gateway.log_id

    @property
    def manufacturer(self) -> str:
        return self.gateway.manufacturer

    @property
    def name(self) -> str:
        return f"{self.gateway.model_name} Gateway"

    @property
    def model(self) -> str:
        return self.gateway.model_name

    @property
    def firmware(self) -> str:
        return self.gateway.firmware

    async def test(self) -> Dict:
        return await OWNSession(gateway=self.gateway, logger=LOGGER).test_connection()

    # =========================
    # EVENT LISTENER (EVENT SESSION)
    # =========================
    # This coroutine maintains a persistent OWNEventSession connection to the gateway.
    # Responsibilities:
    # - Establish and maintain the event session.
    # - Continuously receive asynchronous events from the gateway.
    # - Dispatch events to Home Assistant entities and fire HA bus events.
    # - Implement reconnect logic with exponential backoff on failure.
    # - Implement a watchdog to detect silent event streams.
    #
    # If this loop stops or stalls:
    # - Physical state changes (lights, covers, sensors) will not sync into HA.
    # - No incoming gateway events will be processed.
    async def listening_loop(self):
        self._stop_event_listener = False

        LOGGER.debug("%s Creating listening worker.", self.log_id)
        LOGGER.info("%s Listening loop started.", self.log_id)
        backoff = 1
        max_backoff = 60
        _event_session = None

        # Monotonic timestamps are used for watchdog logic to avoid
        # issues if the system wall clock jumps forward/backward.
        last_event_monotonic = asyncio.get_running_loop().time()
        last_watchdog_monotonic = last_event_monotonic
        watchdog_interval = 300  # seconds (5 min)       

        while not self._stop_event_listener:
            try:
                if _event_session is None:
                    # Establish a fresh event session if none exists.
                    # This is the only place where the inbound channel is created.
                    _event_session = OWNEventSession(gateway=self.gateway, logger=LOGGER)
                    await _event_session.connect()
                    self.is_connected = True
                    backoff = 1
                    LOGGER.info("%s Event session established successfully.", self.log_id)

                # Avoid an infinite await when the gateway resets / half-opens the socket.
                # We wake up periodically to check termination flags and to allow reconnect logic.
                try:
                    message = await asyncio.wait_for(_event_session.get_next(), timeout=30)

                # Timeout here does NOT mean failure.
                # It is intentional to periodically wake the loop,
                # check termination flags, and run watchdog logic.
                except asyncio.TimeoutError:
                    now = asyncio.get_running_loop().time()

                    # Periodic watchdog: prove the loop is still running even if no events arrive.
                    if now - last_watchdog_monotonic >= watchdog_interval:
                        silence = int(now - last_event_monotonic)
                        LOGGER.info("%s Listener alive. No events for %ss.", self.log_id, silence)
                        last_watchdog_monotonic = now

                    LOGGER.debug("%s Listening loop timeout waiting for events (30s).", self.log_id)
                    continue

                LOGGER.debug("%s Message received: `%s`", self.log_id, message)

                last_event_monotonic = asyncio.get_running_loop().time()

                if self.generate_events:
                    if isinstance(message, OWNMessage):
                        _event_content = {"gateway": str(self.gateway.host)}
                        _event_content.update(message.event_content)
                        self.hass.bus.async_fire("myhome_message_event", _event_content)
                    else:
                        self.hass.bus.async_fire("myhome_message_event", {"gateway": str(self.gateway.host), "message": str(message)})

                if not isinstance(message, OWNMessage):
                    LOGGER.warning(
                        "%s Data received is not a message: `%s`",
                        self.log_id,
                        message,
                    )
                elif isinstance(message, OWNEnergyEvent):
                    if SENSOR in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS] and message.entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR]:
                        for _entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES]:
                            if isinstance(
                                self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES][_entity],
                                MyHOMEEntity,
                            ):
                                self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES][_entity].handle_event(message)
                    else:
                        continue
                elif (
                    isinstance(message, OWNLightingEvent)
                    or isinstance(message, OWNAutomationEvent)
                    or isinstance(message, OWNDryContactEvent)
                    or isinstance(message, OWNAuxEvent)
                    or isinstance(message, OWNHeatingEvent)
                ):
                    if not message.is_translation:
                        is_event = False
                        if isinstance(message, OWNLightingEvent):
                            if message.is_general:
                                is_event = True
                                event = "on" if message.is_on else "off"
                                self.hass.bus.async_fire(
                                    "myhome_general_light_event",
                                    {"message": str(message), "event": event},
                                )
                                await asyncio.sleep(0.1)
                                await self.send_status_request(OWNLightingCommand.status("0"))
                            elif message.is_area:
                                is_event = True
                                event = "on" if message.is_on else "off"
                                self.hass.bus.async_fire(
                                    "myhome_area_light_event",
                                    {
                                        "message": str(message),
                                        "area": message.area,
                                        "event": event,
                                    },
                                )
                                await asyncio.sleep(0.1)
                                await self.send_status_request(OWNLightingCommand.status(message.area))
                            elif message.is_group:
                                is_event = True
                                event = "on" if message.is_on else "off"
                                self.hass.bus.async_fire(
                                    "myhome_group_light_event",
                                    {
                                        "message": str(message),
                                        "group": message.group,
                                        "event": event,
                                    },
                                )
                        elif isinstance(message, OWNAutomationEvent):
                            if message.is_general:
                                is_event = True
                                if message.is_opening and not message.is_closing:
                                    event = "open"
                                elif message.is_closing and not message.is_opening:
                                    event = "close"
                                else:
                                    event = "stop"
                                self.hass.bus.async_fire(
                                    "myhome_general_automation_event",
                                    {"message": str(message), "event": event},
                                )
                            elif message.is_area:
                                is_event = True
                                if message.is_opening and not message.is_closing:
                                    event = "open"
                                elif message.is_closing and not message.is_opening:
                                    event = "close"
                                else:
                                    event = "stop"
                                self.hass.bus.async_fire(
                                    "myhome_area_automation_event",
                                    {
                                        "message": str(message),
                                        "area": message.area,
                                        "event": event,
                                    },
                                )
                            elif message.is_group:
                                is_event = True
                                if message.is_opening and not message.is_closing:
                                    event = "open"
                                elif message.is_closing and not message.is_opening:
                                    event = "close"
                                else:
                                    event = "stop"
                                self.hass.bus.async_fire(
                                    "myhome_group_automation_event",
                                    {
                                        "message": str(message),
                                        "group": message.group,
                                        "event": event,
                                    },
                                )
                        if not is_event:
                            if isinstance(message, OWNLightingEvent) and message.brightness_preset:
                                if isinstance(
                                    self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][LIGHT][message.entity][CONF_ENTITIES][LIGHT],
                                    MyHOMEEntity,
                                ):
                                    await self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][LIGHT][message.entity][CONF_ENTITIES][LIGHT].async_update()
                            else:
                                for _platform in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS]:
                                    if _platform != BUTTON and message.entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform]:
                                        for _entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES]:
                                            if (
                                                isinstance(
                                                    self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                                    MyHOMEEntity,
                                                )
                                                and not isinstance(
                                                    self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                                    DisableCommandButtonEntity,
                                                )
                                                and not isinstance(
                                                    self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                                    EnableCommandButtonEntity,
                                                )
                                            ):
                                                self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity].handle_event(message)

                    else:
                        LOGGER.debug(
                            "%s Ignoring translation message `%s`",
                            self.log_id,
                            message,
                        )
                elif isinstance(message, OWNHeatingCommand) and message.dimension is not None and message.dimension == 14:
                    where = message.where[1:] if message.where.startswith("#") else message.where
                    LOGGER.debug(
                        "%s Received heating command, sending query to zone %s",
                        self.log_id,
                        where,
                    )
                    await self.send_status_request(OWNHeatingCommand.status(where))
                elif isinstance(message, OWNCENPlusEvent):
                    event = None
                    if message.is_short_pressed:
                        event = CONF_SHORT_PRESS
                    elif message.is_held or message.is_still_held:
                        event = CONF_LONG_PRESS
                    elif message.is_released:
                        event = CONF_LONG_RELEASE
                    else:
                        event = None
                    self.hass.bus.async_fire(
                        "myhome_cenplus_event",
                        {
                            "object": int(message.object),
                            "pushbutton": int(message.push_button),
                            "event": event,
                        },
                    )
                    LOGGER.info(
                        "%s %s",
                        self.log_id,
                        message.human_readable_log,
                    )
                elif isinstance(message, OWNCENEvent):
                    event = None
                    if message.is_pressed:
                        event = CONF_SHORT_PRESS
                    elif message.is_released_after_short_press:
                        event = CONF_SHORT_RELEASE
                    elif message.is_held:
                        event = CONF_LONG_PRESS
                    elif message.is_released_after_long_press:
                        event = CONF_LONG_RELEASE
                    else:
                        event = None
                    self.hass.bus.async_fire(
                        "myhome_cen_event",
                        {
                            "object": int(message.object),
                            "pushbutton": int(message.push_button),
                            "event": event,
                        },
                    )
                    LOGGER.info(
                        "%s %s",
                        self.log_id,
                        message.human_readable_log,
                    )
                elif isinstance(message, OWNGatewayEvent) or isinstance(message, OWNGatewayCommand):
                    LOGGER.info(
                        "%s %s",
                        self.log_id,
                        message.human_readable_log,
                    )
                else:
                    LOGGER.info(
                        "%s Unsupported message type: `%s`",
                        self.log_id,
                        message,
                    )

            except asyncio.CancelledError:
                break
            except Exception as err:
                LOGGER.warning(
                    "%s Event listener error (%s). Reconnecting in %ss",
                    self.log_id,
                    type(err).__name__,
                    backoff,
                    exc_info=True,
                )
                self.is_connected = False

                try:
                    if _event_session is not None:
                        await _event_session.close()
                except Exception:
                    pass
                _event_session = None

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        # Clean shutdown
        try:
            if _event_session is not None:
                await _event_session.close()
        except Exception:
            pass
        self.is_connected = False

        LOGGER.debug("%s Destroying listening worker.", self.log_id)


    # =========================
    # COMMAND WORKER (COMMAND SESSION)
    # =========================
    # This coroutine maintains a persistent OWNCommandSession connection.
    # Responsibilities:
    # - Consume commands from self.send_buffer queue.
    # - Send commands to the gateway with timeout protection.
    # - Reconnect automatically on timeout or network errors.
    # - Prevent half-open TCP sessions from blocking the worker.
    #
    # If this loop stalls:
    # - Commands from HA (lights, covers, climate, etc.) will not reach the gateway.
    async def sending_loop(self, worker_id: int):
        self._stop_command_workers = False

        LOGGER.debug(
            "%s Creating sending worker %s",
            self.log_id,
            worker_id,
        )

        # Establish outbound command session.
        # Separate from event session to avoid head-of-line blocking.
        _command_session = OWNCommandSession(gateway=self.gateway, logger=LOGGER)
        await _command_session.connect()

        backoff = 1
        max_backoff = 60

        while not self._stop_command_workers:
            try:
                task = await self.send_buffer.get()
                LOGGER.debug(
                    "%s (%s) Message `%s` was successfully unqueued by worker %s.",
                    self.name,
                    self.gateway.host,
                    task["message"],
                    worker_id,
                )

                # Protect send() with timeout to prevent half-open socket hangs.
                # Send with basic timeout protection to avoid half-open hangs
                await asyncio.wait_for(
                    _command_session.send(
                        message=task["message"],
                        is_status_request=task["is_status_request"],
                    ),
                    timeout=30,
                )

                # Update watchdog timestamp
                self._last_command_sent_monotonic = asyncio.get_running_loop().time()

                self.send_buffer.task_done()
                backoff = 1

            except asyncio.TimeoutError:
                LOGGER.warning(
                    "%s Command send timeout. Reconnecting command session.",
                    self.log_id,
                )
                try:
                    await _command_session.close()
                except Exception:
                    pass
                _command_session = OWNCommandSession(gateway=self.gateway, logger=LOGGER)
                await _command_session.connect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

            except asyncio.CancelledError:
                break

            except Exception as err:
                LOGGER.warning(
                    "%s Command session error (%s). Reconnecting in %ss",
                    self.log_id,
                    type(err).__name__,
                    backoff,
                    exc_info=True,
                )
                try:
                    await _command_session.close()
                except Exception:
                    pass
                _command_session = OWNCommandSession(gateway=self.gateway, logger=LOGGER)
                await _command_session.connect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        await _command_session.close()

        LOGGER.debug(
            "%s Destroying sending worker %s",
            self.log_id,
            worker_id,
        )

    # =========================
    # FULL SHUTDOWN (LISTENER + SENDERS)
    # =========================
    # Gracefully stops:
    # - Event listener task
    # - All sending worker tasks
    # Cancels running asyncio tasks and waits for cleanup.
    async def close_listener(self) -> bool:
        LOGGER.info("%s Closing event listener", self.log_id)
        # Signal both loops to terminate and cancel running tasks.
        self._stop_command_workers = True
        self._stop_event_listener = True

        tasks = []
        if self.listening_worker is not None:
            tasks.append(self.listening_worker)
        tasks.extend([t for t in self.sending_workers if t is not None])

        for t in tasks:
            if not t.done():
                t.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self.is_connected = False
        return True

    # =========================
    # EVENT LISTENER ONLY SHUTDOWN
    # =========================
    # Stops only the event session loop.
    # Used when we want to keep command workers alive.
    async def close_listener_only(self) -> bool:
        LOGGER.info("%s Closing event listener only", self.log_id)
        self._stop_event_listener = True
        return True
    
    # =========================
    # PUBLIC SEND API
    # =========================
    # Enqueue a command to be sent to the gateway.
    # Actual transmission is handled asynchronously by sending_loop workers.
    async def send(self, message: OWNCommand):
        await self.send_buffer.put({"message": message, "is_status_request": False})
        LOGGER.debug(
            "%s Message `%s` was successfully queued.",
            self.log_id,
            message,
        )

    # =========================
    # STATUS REQUEST API
    # =========================
    # Enqueue a status request command.
    # Treated the same as send(), but flagged as a status query.
    async def send_status_request(self, message: OWNCommand):
        await self.send_buffer.put({"message": message, "is_status_request": True})
        LOGGER.debug(
            "%s Message `%s` was successfully queued.",
            self.log_id,
            message,
        )
