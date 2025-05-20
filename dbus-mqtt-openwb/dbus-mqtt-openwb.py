#!/usr/bin/env python

from gi.repository import GLib  # pyright: ignore[reportMissingImports]
import platform
import logging
import sys
import os
import json
from time import sleep, time
import paho.mqtt.client as mqtt
import configparser  # for config/ini file
import _thread

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from vedbus import VeDbusService

# --- Load configuration ---
config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.ini')
if not os.path.exists(config_file):
    print(f"ERROR: '{config_file}' not found. Did you copy config.sample.ini? Restarting in 60s.")
    sleep(60)
    sys.exit(1)

config = configparser.ConfigParser()
config.read(config_file)
if config['MQTT']['broker_address'] == 'IP_ADDR_OR_FQDN':
    print("ERROR: Invalid broker address. Restarting in 60s.")
    sleep(60)
    sys.exit(1)

# Logging
loglevel = config.get('DEFAULT', 'logging', fallback='WARNING').upper()
logging.basicConfig(level=getattr(logging, loglevel, logging.WARNING))

# Globals
timeout = int(config.get('DEFAULT', 'timeout', fallback='60'))
last_changed = time()
client = None
dbus_service = None

# MQTT topic base and chargepoint ID for OpenWB 2.x
topic_prefix = config['MQTT']['topic'].rstrip('#').rstrip('/')
chargepoint_id = 5
get_base = f"{topic_prefix}/chargepoint/{chargepoint_id}/get/"

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info('MQTT connected to broker')
        client.subscribe([(f"{topic_prefix}/chargepoint/{chargepoint_id}/get/#", 0),
                          (f"{topic_prefix}/global/ChargeMode", 0)])
    else:
        logging.error(f'MQTT connect failed, rc={rc}')


def on_disconnect(client, userdata, rc):
    logging.warning('MQTT disconnected')
    while True:
        try:
            client.reconnect()
            logging.info('MQTT reconnected')
            return
        except Exception as e:
            logging.error(f'Reconnect failed: {e}, retry in 15s')
            sleep(15)


def on_message(client, userdata, msg):
    global last_changed, dbus_service
    last_changed = time()
    topic = msg.topic
    payload = msg.payload.decode('utf-8', errors='ignore')

    # Global ChargeMode -> /Mode
    if topic == f"{topic_prefix}/global/ChargeMode":
        try:
            mode = int(payload)
            # 2 = PV, else direct
            dbus_service['/Mode'] = 1 if mode == 2 else 0
        except ValueError:
            logging.error(f'Cannot parse ChargeMode: {payload}')
        return

    # Chargepoint data
    if not topic.startswith(get_base) or not dbus_service:
        return

    key = topic[len(get_base):]
    try:
        # simple numeric
        if key == 'power':
            dbus_service['/Ac/Power'] = float(payload)

        elif key == 'powers':
            arr = json.loads(payload)
            for i, v in enumerate(arr):
                path = f'/Ac/L{i+1}/Power'
                dbus_service[path] = float(v)

        elif key == 'voltages':
            arr = json.loads(payload)
            avg = sum(arr) / len(arr)
            dbus_service['/Ac/Voltage'] = avg

        elif key == 'daily_imported':
            dbus_service['/Ac/Energy/Forward'] = float(payload)

        elif key == 'evse_current':  # EVSE charging current
            dbus_service['/Current'] = float(payload)

        elif key == 'plug_state':
            dbus_service['/Status'] = 2 if payload.lower() in ['true', '1'] else 0

        elif key == 'charge_state':
            # active charging -> /StartStop
            dbus_service['/StartStop'] = 1 if payload.lower() in ['true','1'] else 0

        # add more mappings as needed...

    except Exception as e:
        logging.error(f'Error handling {topic}: {e}')


class DbusMqttService:
    def __init__(self, deviceinstance, paths):
        global dbus_service
        svc = VeDbusService(f'com.victronenergy.evcharger.mqtt_wb_{deviceinstance}')
        # Management paths
        svc.add_path('/Mgmt/ProcessName', __file__)
        svc.add_path('/Mgmt/ProcessVersion', 'OpenWB2 Adapter')
        svc.add_path('/Mgmt/Connection', 'MQTT↔DBus')
        # Mandatory battery charger fields
        svc.add_path('/DeviceInstance', deviceinstance)
        svc.add_path('/ProductId', 0xFFFF)
        svc.add_path('/ProductName', config['DEFAULT']['device_name'])
        svc.add_path('/CustomName', config['DEFAULT']['device_name'])
        svc.add_path('/FirmwareVersion', '2.x')
        svc.add_path('/HardwareVersion', 2)
        svc.add_path('/Connected', 1)
        svc.add_path('/UpdateIndex', 0)
        # Status path
        svc.add_path('/Status', None)
        # Add user DBus paths
        for p, meta in paths.items():
            svc.add_path(p, meta['initial'], gettextcallback=meta['textformat'], writeable=True,
                         onchangecallback=self._on_dbus_change)
        dbus_service = svc
        # periodic update index and timeout check
        GLib.timeout_add_seconds(1, self._update)

    def _update(self):
        idx = dbus_service['/UpdateIndex'] + 1
        dbus_service['/UpdateIndex'] = idx if idx <= 255 else 0
        if timeout and (time() - last_changed) > timeout:
            logging.error('MQTT timeout, exiting')
            sys.exit(1)
        return True

    def _on_dbus_change(self, path, value):
        # Publish user-initiated DBus writes back to MQTT set topics
        base = f"{topic_prefix}/chargepoint/{chargepoint_id}/set"
        if not client:
            return False
        if path == '/StartStop':
            topic = f"{base}/chargemode"
            payload = 'instant_charging' if value else 'stop'
        elif path == '/Mode':
            topic = f"{base}/chargemode"
            payload = 'pv_charging' if value == 1 else 'instant_charging'
        elif path == '/SetCurrent':
            topic = f"{base}/current"
            payload = str(value)
        else:
            return False
        client.publish(topic, payload)
        return True


def main():
    global client
    _thread.daemon = True

    from dbus.mainloop.glib import DBusGMainLoop  # pyright: ignore[reportMissingImports]
    DBusGMainLoop(set_as_default=True)

    # Define textformatters and DBus paths
    fmt = {
        'w': lambda v: f"{round(v,1)}W",
        'a': lambda v: f"{round(v,1)}A",
        'v': lambda v: f"{round(v,1)}V",
        'kwh': lambda v: f"{round(v,2)}kWh",
        's': lambda v: f"{v}s",
        't': lambda v: str(v)
    }
    dbus_paths = {
        '/Ac/Power':          {'initial': 0, 'textformat': fmt['w']},
        '/Ac/L1/Power':       {'initial': 0, 'textformat': fmt['w']},
        '/Ac/L2/Power':       {'initial': 0, 'textformat': fmt['w']},
        '/Ac/L3/Power':       {'initial': 0, 'textformat': fmt['w']},
        '/Ac/Energy/Forward': {'initial': 0, 'textformat': fmt['kwh']},
        '/Ac/Voltage':        {'initial': 0, 'textformat': fmt['v']},
        '/Current':           {'initial': 0, 'textformat': fmt['a']},
        '/Mode':              {'initial': 0, 'textformat': fmt['t']},
        '/StartStop':         {'initial': 0, 'textformat': fmt['t']},
        '/SetCurrent':        {'initial': 0, 'textformat': fmt['a']},
        '/MaxCurrent':        {'initial': int(config['WALLBOX']['max']), 'textformat': fmt['a']},
        '/Ac/L1/Power':       {'initial': 0, 'textformat': fmt['w']},
        '/Ac/L2/Power':       {'initial': 0, 'textformat': fmt['w']},
        '/Ac/L3/Power':       {'initial': 0, 'textformat': fmt['w']}
    }

    # Initialize DBus↔MQTT bridge
    DbusMqttService(deviceinstance=int(config['DEFAULT']['device_instance']), paths=dbus_paths)

    # Setup MQTT
    client = mqtt.Client(f"MqttOpenWB_{config['DEFAULT']['device_instance']}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    if config['MQTT'].get('tls_enabled') == '1':
        ca = config['MQTT'].get('tls_path_to_ca') or None
        client.tls_set(ca)
        if config['MQTT'].get('tls_insecure') == '1':
            client.tls_insecure_set(True)
    if (user := config['MQTT'].get('username')):
        client.username_pw_set(user, config['MQTT'].get('password',''))

    client.connect(config['MQTT']['broker_address'], int(config['MQTT']['broker_port']))
    client.loop_start()

    # Run main loop
    GLib.MainLoop().run()

if __name__ == '__main__':
    main()

