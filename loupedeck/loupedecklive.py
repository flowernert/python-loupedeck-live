"""
Main Loupedeck and LoupedeckLive classes.

"""
import glob
import io
import logging
import math
import serial
import sys
import threading
import time
from queue import Queue

from PIL import Image, ImageColor

from constants import BIG_ENDIAN, WS_UPGRADE_HEADER, WS_UPGRADE_RESPONSE
from constants import HEADERS, BUTTONS, HAPTIC, MAX_BRIGHTNESS, DISPLAYS

logger = logging.getLogger("Loupedeck")


MAX_TRANSACTIONS = 256

def print_bytes(buff, begin:int = 18, end:int = 10):
    if buff is None:
        return None
    if len(buff) > 20:
        return f"{buff[0:begin]} ... {buff[-end:]}"
    return f"{buff}"

class Loupedeck:

    def __init__(self):
        self.connection = None
        self.serial = None
        self.version = None
        self.inited = False
        self.running = False

        self._buffer = bytearray(b"")
        self._messages = Queue()

        self.pendingTransactions = [None for _ in range(256)]
        self.transaction_id = 0

        self.callback = None

    @staticmethod
    def list():
        """ Lists serial port names

            :raises EnvironmentError:
                On unsupported or unknown platforms
            :returns:
                A list of the serial ports available on the system
        """
        if sys.platform.startswith("win"):
            ports = [f"COM{i}" for i in range(1, MAX_TRANSACTIONS)]
        elif sys.platform.startswith("linux") or sys.platform.startswith("cygwin"):
            # this excludes your current terminal "/dev/tty"
            ports = glob.glob("/dev/tty[A-Za-z]*")
        elif sys.platform.startswith("darwin"):
            ports = glob.glob("/dev/tty.*")
        else:
            raise EnvironmentError("Unsupported platform")

        result = []
        for port in ports:
            try:
                s = serial.Serial(port)
                s.close()
                result.append(port)
            except (OSError, serial.SerialException):
                pass
        return result

    def get_info(self):
        if self.inited:
            return {
                "version": self.version,
                "serial": self.serial
            }
        return None

class LoupedeckLive(Loupedeck):


    def __init__(self, path:str, baudrate:int, timeout:int):
        Loupedeck.__init__(self)

        self.connection = serial.Serial(port=path, baudrate=baudrate, timeout=timeout)
        self.reading_thread = None  # read
        self.process_thread = None  # messages
        self.touches = {}

        self.handlers = {
            HEADERS["BUTTON_PRESS"]: self.on_button,
            HEADERS["KNOB_ROTATE"]: self.on_rotate,
            HEADERS["SERIAL_IN"]: self.on_serial,
            HEADERS["TICK"]: self.on_tick,
            HEADERS["TOUCH"]: self.on_touch,
            HEADERS["TOUCH_END"]: self.on_touch_end,
            HEADERS["VERSION_IN"]: self.on_version
        }

        self.init()

    def init(self):
        self.init_ws()
        self.info()
        self.test()
        # self.start()

    def init_ws(self):
        self.send(WS_UPGRADE_HEADER, raw=True)
        while True and not self.inited:
            raw_byte = self.connection.readline()
            print(raw_byte)
            if raw_byte == b"\r\n":  # got WS_UPGRADE_RESPONSE
                self.inited = True
            time.sleep(0.1)
        logger.debug(f"init_ws: inited")

    def info(self):
        if self.connection is not None:
            self.do_action(HEADERS["SERIAL_OUT"], track=True)
            self.do_action(HEADERS["VERSION_OUT"], track=True)

    def test(self):
        self.vibrate("ASCEND_MED")
        self.set_brightness(5)
        self.set_button_color("1", "red")
        # self.set_button_color("2", "orange")
        # self.set_button_color("3", "yellow")
        # self.set_button_color("4", "green")
        # self.set_button_color("5", "blue")
        # self.set_button_color("6", "purple")
        # self.set_button_color("7", "white")
        self.test_image()

    # #########################################@
    # Serial Connection
    #
    def send(self, buff, raw = False):
        """
        Send buffer to device

        :param      buffer:  The buffer
        :type       buffer:  { type_description }
        """
        logger.debug(f"send: to send: len={len(buff)}, raw={raw}, {print_bytes(buff)}")
        if not raw:
            prep = None
            if len(buff) > 0x80:
                prep = bytearray(14)
                prep[0] = 0x82
                prep[1] = 0xff
                buff_len = len(buff)
                prep[6:10] = buff_len.to_bytes(4, BIG_ENDIAN)
            else:
                prep = bytearray(6)
                prep[0] = 0x82
                prep[1] = 0x80 + len(buff)
                # prep.insert(2, buff_length.to_bytes(4, "big", False))
            logger.debug(f"send: PREP: len={len(buff)}: {prep}")
            self.connection.write(prep)

        logger.debug(f"send: buff: len={len(buff)}, {print_bytes(buff)}") # {buff},
        self.connection.write(buff)

    # #########################################@
    # Threading
    #
    def start(self):
        self.reading_thread = threading.Thread(target=self._read_serial)
        self.process_thread = threading.Thread(target=self._process_messages)
        self.running = True
        self.process_thread.start()
        self.reading_thread.start()

    def stop(self):
        self.running = False
        for t in threading.enumerate():
            try:
                t.join()
            except RuntimeError:
                pass
        logging.info(f"stop: terminated")

    def _read_serial(self):

        def magic_byte_length_parser(chunk, magicByte = 0x82):
            """
            Build local _buffer and scan it for complete messages.
            Enqueue messages (responses) when reconstituted.

            :param      chunk:      New chunk of data
            :type       chunk:      bytearray
            :param      magicByte:  The magic byte delimiter
            :type       magicByte:  byte
            """
            trace = False
            self._buffer = self._buffer + chunk
            position = self._buffer.find(magicByte)
            while position != -1:
                if trace:
                    logger.debug(f"magic: found {magicByte:x} at {position}")
                #  We need to at least be able to read the length byte
                if len(self._buffer) < position + 2:
                    if trace:
                        logger.debug(f"magic: not enough bytes ({len(self._buffer)}), waiting for more")
                    break
                nextLength = self._buffer[position + 1]
                #  Make sure we have enough bytes to meet self length
                expectedEnd = position + nextLength + 2
                if len(self._buffer) < expectedEnd:
                    if trace:
                        logger.debug(f"magic: not enough bytes for message ({len(self._buffer)}, exp={expectedEnd}), waiting for more")
                    break
                if trace:
                    logger.debug(f"magic: message from {position + 2} to {expectedEnd} (len={nextLength}), enqueueing")
                self._messages.put(self._buffer[position+2:expectedEnd])
                self._buffer = self._buffer[expectedEnd:]
                position = self._buffer.find(magicByte)

        logger.debug("_read_serial: starting")

        while self.running and self.inited:
            raw_byte = b""
            if self.inited:
                raw_byte = self.connection.read()
            else:
                raw_byte = self.connection.readline()
            if raw_byte != b"":
                # logger.debug(f"raw_byte: {raw_byte}")
                magic_byte_length_parser(raw_byte)

        logger.debug("_read_serial: terminated")

    def _process_messages(self):

        logger.debug("_process_messages: starting")

        while self.running:
            while not self._messages.empty():
                buff = self._messages.get()
                logger.debug(f"_process_messages: {buff}")
                try:
                    header = int.from_bytes(buff[0:2], BIG_ENDIAN)
                    handler = self.handlers[header] if header in self.handlers else None
                    transaction_id = buff[2]
                    logger.debug(f"_process_messages: transaction_id {transaction_id}, {header:x}")
                    response = handler(buff[3:]) if handler is not None else buff
                    resolver = self.pendingTransactions[transaction_id] if transaction_id in self.pendingTransactions else None
                    if resolver is not None:
                        resolver(transaction_id, response)
                    else:
                        self.on_default_callback(transaction_id, response)
                except:
                    logger.error(f"_process_messages: exception:", exc_info=1)
                    logger.error(f"_process_messages: continuing")

            time.sleep(1)

        logger.debug("_process_messages: terminated")

    # #########################################@
    # Callbacks
    #
    def do_action(self, action, data:bytearray = None, track:bool = False):
        if self.connection is None:
            return

        if data is not None and type(data) != bytearray and type(data) != bytes:
            data = data.to_bytes(1, BIG_ENDIAN)
            logger.debug(f"do_action: converted data") #  '{data}'")

        logger.debug(f"do_action: {action:04x}, {print_bytes(data)}")
        self.transaction_id = (self.transaction_id + 1) % MAX_TRANSACTIONS
        if self.transaction_id == 0:  # Skip transaction ID's of zero since the device seems to ignore them
             self.transaction_id = self.transaction_id + 1
        header = action.to_bytes(2, BIG_ENDIAN) + self.transaction_id.to_bytes(1, BIG_ENDIAN)
        logger.debug(f"do_action: id={self.transaction_id}, header={header}, track={track}")
        payload = header
        if data is not None:
            logger.debug(f"do_action: has data {payload} + '{print_bytes(data)}'")
            payload = payload + data

        if track:
            logger.debug(f"do_action: tracking {self.transaction_id}")
            self.pendingTransactions[self.transaction_id] = self.on_default_callback
        self.send(payload)

    def on_serial(self, serial):
        self.serial = serial.strip()
        logger.info(f"Serial number: {self.serial}")

    def on_version(self, version):
        self.version = f"{version[0]}.{version[1]}.{version[2]}"
        logger.info(f"Version: {self.version}")

    def on_button(self, buff):
        idx = BUTTONS[buff[0]]
        event = 'down' if buff[1] == 0x00 else 'up'
        if self.callback:
            self.callback({
                "type": "button",
                "idx": idx,
                "state": event
            })
        logger.debug(f"on_button: {idx}, {event}")

    def on_rotate(self, buff):
        idx = BUTTONS[buff[0]]
        event = "right" if buff[1] == 0x01 else "left"
        if self.callback:
            self.callback({
                "type": "rotate",
                "idx": idx,
                "state": event
            })
        logger.debug(f"on_rotate: {idx}, {event}")

    def on_touch(self, buff, event="touchmove"):
        x = int.from_bytes(buff[1:3], BIG_ENDIAN)
        y = int.from_bytes(buff[3:5], BIG_ENDIAN)
        idx = buff[5]

        # Determine target
        screen = "center"
        if x < 60:
            screen = "left"
        elif x > 420:
            screen = "right"

        key = None
        if screen == "center":
            column = math.floor((x - 60) / 90)
            row = math.floor(y / 90)
            key = row * 4 + column

        # Create touch
        touch = {
            "x": x,
            "y": y,
            "idx": idx,
            "screen": screen,
            "key": key,
            "type": event
        }
        if event == "touchmove":
            if idx not in self.touches:
                touch["type"] = "touchstart"
                self.touches[idx] = touch
        else:
            del self.touches[idx]

        if self.callback:
            self.callback(touch)
        logger.debug(f"on_touch: {event}, {buff}")

    def on_touch_end(self, buff):
        self.on_touch(buff, event="touchend")

    def on_tick(self, buff):
        logger.debug(f"on_tick: {buff}")

    def on_default_callback(self, transaction_id: int, response):
        logger.debug(f"{transaction_id}: {response}")
        self.pendingTransactions[transaction_id] = None

    def set_callback(self, callback: callable):
        """
        This is the user's callback called when action
        occurred on the Loupedeck device

        :param      callback:  The callback
        :type       callback:  Function
        """
        self.callback = callback

    # #########################################@
    # Loupedeck Functions
    #
    def set_brightness(self, brightness: int):
        """
        Set brightness, from 0 (dark) to 10.
        """
        if brightness < 1:
            logger.warning(f"set_brightness: brightness set to 0")
            brightness = 0
        if brightness > MAX_BRIGHTNESS:
            brightness = MAX_BRIGHTNESS
        self.do_action(HEADERS["SET_BRIGHTNESS"], brightness.to_bytes(1, BIG_ENDIAN))
        logger.debug(f"set_brightness: sent {brightness}")

    def set_button_color(self, name: str, color):
        keys = list(filter(lambda k: BUTTONS[k] == name, BUTTONS))
        if len(keys) != 1:
            logger.info(f"set_button_color: invalid button key {name}")
        key = keys[0]
        (r, g, b) = ImageColor.getrgb(color)
        data = bytearray([key, r, g, b])
        self.do_action(HEADERS["SET_COLOR"], data)
        logger.debug(f"set_button_color: sent {name}, {color}")

    def vibrate(self, pattern = "SHORT"):
        if pattern not in HAPTIC.keys():
            logger.error(f"vibrate: invalid pattern {pattern}")
            return
        self.do_action(HEADERS["SET_VIBRATION"], HAPTIC[pattern])
        logger.debug(f"vibrate: sent {pattern}")

    # Image display functions
    #
    def refresh(self, display:int):
        display_info = DISPLAYS[display]
        self.do_action(HEADERS["DRAW"], display_info["id"], track=True)
        logger.error("refresh: refreshed")

    def draw_buffer(self, buff, display:str, width: int = None, height: int = None, x:int = 0, y:int = 0, auto_refresh:bool = True):
        display_info = DISPLAYS[display]
        if width is None:
            width = display_info["width"]
        if height is None:
            height = display_info["height"]
        expected = width * height * 2
        if len(buff) != expected:
            logger.error(f"draw_buffer: invalid buffer {len(buff)}, expected={expected}")

        logger.debug(f"draw_buffer: o={x},{y}, dim={width},{height}")

        header = x.to_bytes(2, BIG_ENDIAN)
        header = header + y.to_bytes(2, BIG_ENDIAN)
        header = header + width.to_bytes(2, BIG_ENDIAN)
        header = header + height.to_bytes(2, BIG_ENDIAN)
        payload = display_info["id"] + header + buff
        self.do_action(HEADERS["WRITE_FRAMEBUFF"], payload, track=True)
        logger.error(f"draw_buffer: buffer sent {len(buff)} bytes")
        if auto_refresh:
            self.refresh(display)

    def draw_image(self, image, display:str, width: int = None, height: int = None, x:int = 0, y:int = 0, auto_refresh:bool = True):
        # Loupedeck uses 16-bit (5-6-5) LE RGB colors
        def rgb565(r, g, b, a=255):
            p1 = r & 248  # 11111000
            p1d = p1 >> 3    # display

            p2a = g & 224 # 11100000
            p2a = p2a >> 5
            p2b = g & 28  # 00011100
            p2b = p2b << 3
            p2bd = p2b >> 5  # display

            p3 = b & 248
            p3 = p3 >> 3

            b1 = p1 + p2a
            b2 = p2b + p3
            b = b1 * 256 + b2
            # if i == j:
            #     print(f"{i},{j}: ({r}={r:08b}, {g}={g:08b}, {b}={b:08b}) => ({p1d:05b}|{p2a:03b}|{p2bd:03b}|{p3:05b}) => ({b1:08b}{b2:08b}) = {b:016b}")
            return b

        buff = bytearray()
        if width is None:
            width = image.width
        if height is None:
            height = image.height

        for j in range(height):
            for i in range(width):
                p = image.getpixel((i, j))
                b16 = rgb565(*p)
                buff = buff + b16.to_bytes(2, "little") # little?? really
        self.draw_buffer(buff, display=display, width=width, height=height, x=x, y=y, auto_refresh=auto_refresh)

    def draw_screen(self, image, display:str, auto_refresh:bool = True):
        if type(image) == bytearray:
            self.draw_buffer(image, display=display, auto_refresh=auto_refresh)
        else: # type(image) == PIL.Image.Image
            self.draw_image(image, display=display, auto_refresh=auto_refresh)

    def set_key_image(self, idx: int, image):
        # Get offset x/y for key index
        width = 90
        height = 90
        x = idx % 4 * width
        y = math.floor(idx / 4) * height
        if type(image) == bytearray:
            self.draw_buffer(image, display="center", width=width, height=height, x=x, y=y, auto_refresh=True)
        else: # type(image) == PIL.Image.Image
            self.draw_image(image, display="center", width=width, height=height, x=x, y=y, auto_refresh=True)


    def test_image(self):
        # image = Image.new("RGBA", (360, 270), "cyan")
        with open("yumi.jpg", "rb") as infile:
            image = Image.open(infile).convert("RGBA")
            self.draw_image(image, display="center")
        image2 = Image.new("RGBA", (90, 90), "blue")
        self.set_key_image(6, image2)

