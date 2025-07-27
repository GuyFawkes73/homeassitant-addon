#!/usr/bin/python
#
# file: callattendant.py
#
# Copyright 2018 Bruce Schubert <bruce@emxsys.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import json
import os
import sys
import queue
import sqlite3
import time
import threading
from datetime import datetime
from pprint import pprint
from shutil import copyfile

from config import Config
from screening.calllogger import CallLogger
from screening.callscreener import CallScreener
from hardware.modem import Modem
from messaging.voicemail import VoiceMail
import userinterface.webapp as webapp


class CallAttendant(object):
    """The CallAttendant provides call logging and call screening services."""

    def __init__(self, config):
        """
        The constructor initializes and starts the Call Attendant.
            :param config:
                the application config dict
        """
        # The application-wide configuration
        self.config = config

        # Thread synchonization object
        self._stop_event = threading.Event()
        # Evento per segnalare che la connessione MQTT è avvenuta
        self._mqtt_connected_event = threading.Event()

        # Open the database
        if self.config["TESTING"]:
            self.db = sqlite3.connect(":memory:")
        else:
            self.db = sqlite3.connect(self.config['DB_FILE'])

        # Create a synchronized queue for incoming callers from the modem
        self._caller_queue = queue.Queue()

        #  Create (and open) the modem
        self.modem = Modem(self.config)
        self.config["MODEM_ONLINE"] = self.modem.is_open  # signal the webapp not online

        # Screening subsystem
        self.logger = CallLogger(self.db, self.config)
        self.screener = CallScreener(self.db, self.config)

        # Messaging subsystem
        self.voice_mail = VoiceMail(self.db, self.config, self.modem)

        # Start the User Interface subsystem (Flask)
        # Skip if we're running functional tests, because when testing
        # we use a memory database which can't be shared between threads.
        if not self.config["TESTING"]:
            print("Starting the Flask webapp")
            webapp.start(self.config, self._mqtt_connected_event, self.modem, self)
            self.start_discovery_renewal()
            

    def publish_discovery(self):
 
        discovery_payload_sensor = {
    "name": "Chiamata Telefono Casa",
    "unique_id": "call_messenger_home_phone",
    "state_topic": "homeassistant/sensor/home_phone_call/state",
    "value_template": "{{ value_json.status }}",
    "json_attributes_topic": "homeassistant/sensor/home_phone_call/state",
    "device": {
        "identifiers": ["home_phone_messenger_device"],
        "name": "Messaggero Telefono Casa",
        "model": "Call Messenger",
        "manufacturer": "Custom"
    },
    "icon": "mdi:phone-incoming",
}


        try:
            if webapp.mqtt.client and webapp.mqtt.client.is_connected():
                webapp.mqtt.publish(
                    topic="homeassistant/sensor/call_messenger_home_phone/config",
                    payload=json.dumps(discovery_payload_sensor),
                    retain=True
                )

                if self.config.get("DEBUG"):
                    print("Discovery MQTT published")
        except Exception as e:
            if self.config.get("DEBUG"):
                print(f"Discovery MQTT publish failed: {e}")

        discovery_payload_button = {
    "name": "Riaggancia Telefono Casa",
    "unique_id": "hangup_home_phone_button",
    "command_topic": "homeassistant/button/home_phone_hangup/command",
    "payload_press": "HANGUP",
    "device": {
        "identifiers": ["home_phone_messenger_device"]
    },
    "icon": "mdi:phone-hangup",
    "component": "button"
}


        try:
            if webapp.mqtt.client and webapp.mqtt.client.is_connected():
                webapp.mqtt.publish(
                    topic="homeassistant/button/hangup_home_phone_button/config",
                    payload=json.dumps(discovery_payload_button),
                    retain=True
                )

                if self.config.get("DEBUG"):
                    print("Discovery MQTT published")
        except Exception as e:
            if self.config.get("DEBUG"):
                print(f"Discovery MQTT publish failed: {e}")

    def start_discovery_renewal(self, interval=3600):
        def renew():
            # Attendi che la connessione MQTT sia stabilita
            if self.config.get("DEBUG"):
                print("Discovery thread in attesa della connessione MQTT...")
            self._mqtt_connected_event.wait()
            if self.config.get("DEBUG"):
                print("MQTT connesso. Invio del primo messaggio di discovery.")

            self.publish_discovery()  # Pubblica subito dopo la connessione
            while not self._stop_event.is_set():
                self._stop_event.wait(interval)
                if not self._stop_event.is_set():
                    self.publish_discovery()
        t = threading.Thread(target=renew, daemon=True)
        t.start()

    def handle_caller(self, caller):
        """
        A callback function used by the modem that places the given
        caller object into the synchronized queue for processing by the
        run method.
            :param caller:
                a dict object with caller ID information
        """
        if self.config["DEBUG"]:
            print("Adding to caller queue:")
            pprint(caller)

        self._caller_queue.put(caller)

    def run(self):
        """
        Processes incoming callers by logging, screening, blocking
        and/or recording messages.
            :returns: exit code 1 on error otherwise 0
        """
        # Get relevant config settings
        screening_mode = self.config['SCREENING_MODE']
        blocked = self.config.get_namespace("BLOCKED_")
        screened = self.config.get_namespace("SCREENED_")
        permitted = self.config.get_namespace("PERMITTED_")
        blocked_greeting_file = blocked['greeting_file']
        screened_greeting_file = screened['greeting_file']
        permitted_greeting_file = permitted['greeting_file']

        # Instruct the modem to start feeding calls into the caller queue
        self.modem.start(self.handle_caller)

        # If testing, allow queue to be filled before processing for clean, readable logs
        if self.config["TESTING"]:
            time.sleep(1)

        # Process incoming calls
        exit_code = 0
        caller = {}
        print("Waiting for call...")
        while not self._stop_event.is_set():
            try:
                # Wait (blocking) for a caller
                try:
                    caller = self._caller_queue.get(True, 3.0)
                except queue.Empty:
                    continue

                # An incoming call has occurred, log it
                number = caller["NMBR"]
                print("Incoming call from {}".format(number))

                # Vars used in the call screening
                caller_permitted = False
                caller_screened = False
                caller_blocked = False
                action = ""
                reason = ""

                # Check the whitelist
                if "whitelist" in screening_mode:
                    print("> Checking whitelist(s)")
                    is_whitelisted, name, reason = self.screener.is_whitelisted(caller)
                    if is_whitelisted:
                        if name:
                            # Update the caller's name with the name from the whitelist
                            caller["NAME"] = name
                        caller_permitted = True
                        action = "Permitted"

                # Now check the blacklist if not preempted by whitelist
                if not caller_permitted and "blacklist" in screening_mode:
                    print("> Checking blacklist(s)")
                    is_blacklisted, reason = self.screener.is_blacklisted(caller)
                    if is_blacklisted:
                        caller_blocked = True
                        action = "Blocked"

                if not caller_permitted and not caller_blocked:
                    caller_screened = True
                    action = "Screened"

                # Log every call to the database (and console)
                call_no = self.logger.log_caller(caller, action, reason)
                print("--> {} {}: {}".format(number, action, reason))

                # Gather the data used to answer the call
                if caller_permitted:
                    actions = permitted["actions"]
                    greeting = permitted_greeting_file
                    rings_before_answer = permitted["rings_before_answer"]
                elif caller_screened:
                    actions = screened["actions"]
                    greeting = screened_greeting_file
                    rings_before_answer = screened["rings_before_answer"]
                elif caller_blocked:
                    actions = blocked["actions"]
                    greeting = blocked_greeting_file
                    rings_before_answer = blocked["rings_before_answer"]

                # Waits for the callee to answer the phone, if configured to do so.
                ok_to_answer = self.wait_for_rings(rings_before_answer)

                # Answer the call!
                if ok_to_answer and "answer" in actions:
                    self.answer_call(actions, greeting, call_no, caller)
                else:
                    self.ignore_call(caller)

                print("Waiting for next call...")

            except KeyboardInterrupt:
                print("** User initiated shutdown")
                self.stop()
            except Exception as e:
                pprint(e)
                print("** Error running callattendant")
                self.stop()
                exit_code = 1
        return exit_code

    def stop(self):
        self._stop_event.set()

    def shutdown(self):
        """
        Shuts down threads and releases resources.
        """
        print("Shutting down...")
        print("-> Stopping modem")
        self.modem.stop()
        print("-> Stopping voice mail")
        self.voice_mail.stop()
        print("Shutdown finished")

    def answer_call(self, actions, greeting, call_no, caller):
        """
        Answer the call with the supplied actions, e.g, voice mail,
        record message, or simply pickup and hang up.
            :param actions:
                A tuple containing the actions to take for this call
            :param greeting:
                The wav file to play to the caller upon answering
            :param call_no:
                The unique call number identifying this call
            :param caller:
                The caller ID data
        """
        # Go "off-hook" - Acquires a lock on the modem - MUST follow with hang_up()
        if self.modem.pick_up():
            try:
                # Play greeting
                if "greeting" in actions:
                    print("> Playing greeting...")
                    self.modem.play_audio(greeting)

                # Record message
                if "record_message" in actions:
                    print("> Recording message...")
                    self.voice_mail.record_message(call_no, caller)

                # Enter voice mail menu
                elif "voice_mail" in actions:
                    print("> Starting voice mail...")
                    self.voice_mail.voice_messaging_menu(call_no, caller)

            except RuntimeError as e:
                print("** Error answering a call: {}".format(e))

            finally:
                # Go "on-hook"
                self.modem.hang_up()

    def force_hang_up(self):
        """
        Forza la chiusura della chiamata. Questo metodo è sicuro da chiamare
        da altri thread (es. webapp) perché gestisce il ciclo pick_up/hang_up.
        """
        # Questo metodo non fa un vero "pick_up" per parlare, ma usa la logica
        # di pick_up() per acquisire il lock del modem in modo sicuro prima
        # di chiamare hang_up().
        if self.modem.pick_up():
            try:
                # Una volta acquisito il lock, possiamo immediatamente riagganciare.
                pass
            finally:
                self.modem.hang_up()

    def ignore_call(self, caller):
        """
        Ignore (do not answer) the call.
            :param caller:
                The caller ID data
        """
        # Pubblica su MQTT lo stato della chiamata in arrivo (sta squillando)
        try:
            topic = 'homeassistant/sensor/home_phone_call/state'

            name = caller.get("NAME")
            payload = {
                "status": "ringing",
                "caller_number": caller.get("NMBR"),
                "timestamp": datetime.now().isoformat()
            }

            if name and name.strip() and name != "Unknown":
                payload["caller_name"] = name
        
            # Pubblica solo se MQTT è inizializzato
            if webapp.mqtt.client and webapp.mqtt.client.is_connected():
                webapp.mqtt.publish(topic, json.dumps(payload), retain=True)
                if self.config.get("DEBUG"):
                    print(f"MQTT published to {topic}: {json.dumps(payload)}")
                
                # Funzione per resettare lo stato dopo un timeout
                def reset_state_after_timeout(topic, delay_seconds=10):
                    time.sleep(delay_seconds)
                    idle_payload = {
                        "status": "idle",
                        "timestamp": datetime.now().isoformat()
                    }
                    if webapp.mqtt.client and webapp.mqtt.client.is_connected():
                        webapp.mqtt.publish(topic, json.dumps(idle_payload), retain=True)
                        if self.config.get("DEBUG"):
                            print(f"MQTT state reset to idle on topic {topic}")

                # Avvia il reset in un thread separato per non bloccare l'esecuzione
                reset_thread = threading.Thread(target=reset_state_after_timeout, args=(topic,))
                reset_thread.start()

        except Exception as e:
            if self.config.get("DEBUG"):
                print(f"MQTT publish failed: {e}")



    def wait_for_rings(self, rings_before_answer):
        """
        Waits for the given number of rings to occur.
        :param rings_before_answer:
            the number of rings to wait for.
        :return:
            True if the ring count meets or exceeds the rings before answer;
            False if the rings stop or if another call comes in.
        """
        # In North America, the standard ring cadence is "2-4", or two seconds
        # of ringing followed by four seconds of silence (33% Duty Cycle).
        RING_CADENCE = 6.0  # secs
        RING_WAIT_SECS = RING_CADENCE + (RING_CADENCE * 0.5)
        ok_to_answer = True
        ring_count = 1  # Already had at least 1 ring to get here
        last_ring = datetime.now()
        while ring_count < rings_before_answer:
            if not self._caller_queue.empty():
                # Skip this call and process the next one
                print(" > > > Another call has come in")
                ok_to_answer = False
                break
            # Wait for a ring
            elif self.modem.ring_event.wait(1.0):
                # Increment the ring count and time of last ring
                ring_count += 1
                last_ring = datetime.now()
                print(" > > > Ring count: {}".format(ring_count))
            # On wait timeout, test for ringing stopped
            elif (datetime.now() - last_ring).total_seconds() > RING_WAIT_SECS:
                # Assume ringing has stopped before the ring count
                # was reached because either the callee answered or caller hung up.
                print(" > > > Ringing stopped: Caller hung up or callee answered")
                ok_to_answer = False
                break
        return ok_to_answer


def make_config(filename=None, datapath=None, create_folder=False):
    """
    Creates the config dictionary for this application/module.
        :param filename:
            The filename of a python configuration file.
            This can either be an absolute filename or a filename
            relative to the datapath folder.
        :param datapath:
            A folder for the database, messages and configuration files.
            It will be created if it doesn't exist.
        :return:
            A config dict object
    """
    # Establish the default configuration settings
    root_path = os.path.dirname(os.path.realpath(__file__))
    data_path = datapath
    if data_path is None:
        # The default data_path is a hidden folder at the root of the user home folder
        data_path = os.path.expanduser("~/.callattendant")
    if create_folder:
        # Create the data-path folder hierachy with a default app.cfg file
        if not os.path.isdir(data_path):
            print("The DATA_PATH folder is not present. Creating {}".format(data_path))
            os.makedirs(data_path)

            print("Adding a default 'app.cfg' configuration file.")
            copyfile(os.path.join(root_path, "app.cfg.example"), os.path.join(data_path, "app.cfg"))
    else:
        if not os.path.isdir(data_path):
            print("Error: The data path ({}) does not exist.".format(data_path))
            print("Run the app with the --create-folder option or create the data folder before retrying.")
            show_syntax()
            sys.exit(2)

    # Create the default configuration...
    config = Config(root_path, data_path)
    if filename is not None:
        # ... and now load values from the supplied config file
        config.from_pyfile(filename)
        config["CONFIG_FILE"] = filename

    # Build absolute paths for all the file based settings
    config.normalize_paths()
    # Initialize the data_path folder contents using the normalized paths
    init_data_path(config)
    # Always print the configuration
    config.pretty_print()

    return config


def init_data_path(config):
    """
    Ensures requisite folders exist.
        :param config:
            The application's config dict object
    """
    # Create the sub-folder used for the message wav files
    msgpath = config["VOICE_MAIL_MESSAGE_FOLDER"]
    if not os.path.isdir(msgpath):
        print("The VOICE_MAIL_MESSAGE_FOLDER folder is not present. Creating {}".format(msgpath))
        os.mkdir(msgpath)

    # Create a softlink/symlink to messages within the static folder
    # so that the HTML pages have access to the .wav files
    symlink_path = os.path.join(config.root_path, "userinterface/static/messages")

    # TODO validate and/or delete the existing link and then create a new one for this session
    # TODO could use os.readlink to test the viability of the symlink in case the data_path moved
    if not os.path.exists(symlink_path):
        print("The VOICE_MAIL_MESSAGE_FOLDER symlink is not present. Creating {} symlink".format(symlink_path))
        os.symlink(config["VOICE_MAIL_MESSAGE_FOLDER"], symlink_path)


def get_args(argv):
    """Get and validate the command line arguments.
        :param argv:
            sys.argv from main
        :return:
            string: config filename,
            string: datapath folder,
            boolean: create folder flag
    """
    import sys
    import getopt
    config_file = None
    data_path = None
    create_folder = False
    try:
        opts, args = getopt.getopt(argv[1:], "hc:d:f", ["help", "config=", "data-path=", "create-folder"])
        if args:
            raise getopt.GetoptError("unhandled arguments: {}".format(args))
    except getopt.GetoptError as e:
        print("Error: {}".format(e))
        show_syntax()
        sys.exit(2)
    for opt, arg in opts:
        if opt in ("-h", "--help"):
            show_syntax()
            sys.exit()
        elif opt in ("-c", "--config"):
            config_file = arg
        elif opt in ("-d", "--data-path"):
            data_path = arg
        elif opt in ("-f", "--create-folder"):
            create_folder = True
        else:
            raise RuntimeError("Invalid command line option: {} {}".format(opt, arg))

    return config_file, data_path, create_folder


def show_syntax():
    """
    Print the command line syntax.
    """
    print("Usage: callattendant --config [FILE] --data-path [FOLDER]")
    print("Options:")
    print("-c, --config [FILE]\t\t load a python configuration file")
    print("-d, --data-path [FOLDER]\t path to data and configuration files")
    print("-f, --create-folder\t\t create the data-path folder if it does not exist")
    print("-h, --help\t\t\t displays this help text")


def main(argv):
    """
    Create and run the call attendent application.
        :param argv:
            The command line arguments, e.g., --config [FILE] --data-path [FOLDER]
    """
    # Process command line arguments
    config_file, data_path, create_folder = get_args(argv)
    print("Command line options:")
    print("  --config={}".format(config_file))
    print("  --data-path={}".format(data_path))
    print("  --create-folder={}".format(create_folder))

    # Create the application-wide config dict
    config = make_config(config_file, data_path, create_folder)

    # Ensure all specified files exist and that values are conformant
    if not config.validate():
        print("ERROR: Configuration is invalid. Please check {}".format(config_file))
        return 1

    # Create and start the application
    app = CallAttendant(config)
    exit_code = 0
    try:
        exit_code = app.run()
    finally:
        app.shutdown()
    return exit_code


if __name__ == '__main__':

    sys.exit(main(sys.argv))
