# SPDX-License-Identifier: GPL-3.0-or-later
#
# Vendored from turing-smart-screen-python - a Python system monitor and
# library for USB-C displays like Turing Smart Screen or XuanFang
# https://github.com/mathoudebine/turing-smart-screen-python/
#
# Copyright (C) 2021 Matthieu Houdebine (mathoudebine)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Trimmed for mister_turing_client: only the pieces LcdCommRevA needs are
# kept (unused revisions' code was never here to begin with - this file
# is otherwise unmodified apart from import paths).

import copy
import math
import os
import platform
import queue
import sys
import threading
import time
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import Tuple, List, Optional, Dict

import serial
from PIL import Image, ImageDraw, ImageFont

from .log import logger
from .color import Color, parse_color


class Orientation(IntEnum):
    PORTRAIT = 0
    LANDSCAPE = 2
    REVERSE_PORTRAIT = 1
    REVERSE_LANDSCAPE = 3


# The screen resets itself when the program starts, so its COM port can disappear or change for a
# few seconds: opening it is retried instead of giving up (and exiting) on the first failure.
SERIAL_OPEN_ATTEMPTS = 10
SERIAL_OPEN_RETRY_DELAY = 1  # seconds


class LcdComm(ABC):
    def __init__(self, com_port: str = "AUTO", display_width: int = 320, display_height: int = 480,
                 update_queue: Optional[queue.Queue] = None):
        self.lcd_serial = None

        # String containing absolute path to serial port e.g. "COM3", "/dev/ttyACM1" or "AUTO" for auto-discovery
        # Ignored for USB HID screens
        self.com_port = com_port

        # Display always start in portrait orientation by default
        self.orientation = Orientation.PORTRAIT
        # Display width in default orientation (portrait)
        self.display_width = display_width
        # Display height in default orientation (portrait)
        self.display_height = display_height

        # Queue containing the serial requests to send to the screen. An external thread should run to process requests
        # on the queue. If you want serial requests to be done in sequence, set it to None
        self.update_queue = update_queue

        # Mutex to protect the queue in case a thread want to add multiple requests (e.g. image data) that should not be
        # mixed with other requests in-between
        self.update_queue_mutex = threading.Lock()

        # Create a cache to store opened images, to avoid opening and loading from the filesystem every time
        self.image_cache = {}  # { key=path, value=PIL.Image }

        # Create a cache to store opened fonts, to avoid opening and loading from the filesystem every time
        self.font_cache: Dict[
            Tuple[str, int],  # key=(font, size)
            ImageFont.FreeTypeFont  # value= a loaded freetype font
        ] = {}

    def get_width(self) -> int:
        if self.orientation == Orientation.PORTRAIT or self.orientation == Orientation.REVERSE_PORTRAIT:
            return self.display_width
        else:
            return self.display_height

    def get_height(self) -> int:
        if self.orientation == Orientation.PORTRAIT or self.orientation == Orientation.REVERSE_PORTRAIT:
            return self.display_height
        else:
            return self.display_width

    def openSerial(self):
        # self.com_port is kept as configured ("AUTO" or a port name): on AUTO the port is
        # detected again at every attempt, since it can change while the screen resets.
        for attempt in range(1, SERIAL_OPEN_ATTEMPTS + 1):
            com_port = self.com_port
            if com_port == 'AUTO':
                com_port = self.auto_detect_com_port()
                if not com_port:
                    logger.warning(
                        f"Cannot find COM port automatically, retrying ({attempt}/{SERIAL_OPEN_ATTEMPTS})")
                    time.sleep(SERIAL_OPEN_RETRY_DELAY)
                    continue
                logger.debug(f"Auto detected COM port: {com_port}")
            else:
                logger.debug(f"Static COM port: {com_port}")

            try:
                # write_timeout bounds serial_write() the same way timeout
                # already bounds reads. Without it, a write blocks
                # indefinitely if the display stops draining its input
                # buffer (confirmed in the field: the whole app - polling,
                # rendering, and artwork fetching, all inline in one loop -
                # froze for 6+ minutes at a stretch with zero exceptions
                # anywhere, because this call never returned). With a
                # timeout set, WriteLine()'s existing SerialTimeoutException
                # handler (below) actually gets a chance to run instead of
                # that code being unreachable.
                self.lcd_serial = serial.Serial(com_port, 115200, timeout=1, write_timeout=5, rtscts=True)
                return
            except Exception as e:
                logger.warning(
                    f"Cannot open COM port {com_port}: {e} - retrying ({attempt}/{SERIAL_OPEN_ATTEMPTS})")
                time.sleep(SERIAL_OPEN_RETRY_DELAY)

        logger.error(
            f"Cannot open COM port after {SERIAL_OPEN_ATTEMPTS} attempts. If the screen is connected, run "
            f"Configuration again and select the COM port manually")
        try:
            sys.exit(0)
        except:
            os._exit(0)

    def closeSerial(self):
        if self.lcd_serial is not None:
            self.lcd_serial.close()

    def serial_write(self, data: bytes):
        # Timeout/error handling lives here, not in WriteLine(), because
        # several call sites (e.g. SetOrientation() in lcd_comm_rev_a.py)
        # write directly through this method rather than through WriteLine -
        # confirmed the hard way: adding write_timeout to the Serial object
        # without this turned an indefinite hang into an uncaught
        # SerialTimeoutException crashing the whole app on startup instead
        # (SetOrientation() is called from Clear(), which runs before the
        # main loop even starts).
        assert self.lcd_serial is not None
        try:
            self.lcd_serial.write(data)
        except serial.SerialTimeoutException:
            logger.warning("(serial_write) Too fast! Slow down!")
        except serial.SerialException:
            logger.error(
                "SerialException: Failed to send serial data to device. Closing and reopening COM port before retrying once.")
            self.closeSerial()
            time.sleep(1)
            self.openSerial()
            self.lcd_serial.write(data)

    def serial_read(self, size: int) -> bytes:
        assert self.lcd_serial is not None
        return self.lcd_serial.read(size)

    def serial_readall(self) -> bytes:
        assert self.lcd_serial is not None
        return self.lcd_serial.readall()

    def serial_flush_input(self):
        if self.lcd_serial is not None:
            self.lcd_serial.reset_input_buffer()

    def WriteData(self, byteBuffer: bytearray):
        self.WriteLine(bytes(byteBuffer))

    def SendLine(self, line: bytes):
        if self.update_queue:
            # Queue the request. Mutex is locked by caller to queue multiple lines
            self.update_queue.put((self.WriteLine, [line]))
        else:
            # If no queue for async requests: do request now
            self.WriteLine(line)

    def WriteLine(self, line: bytes):
        # Timeout/reconnect handling lives in serial_write() now - see its
        # own comment - so it applies to every caller, not just this one.
        self.serial_write(line)
        if platform.system() == "Darwin":
            # macOS needs the serial buffer to be flushed regularly to avoid bitmap corruption on the display
            # See https://github.com/mathoudebine/turing-smart-screen-python/issues/7
            self.lcd_serial.flush()

    def ReadData(self, readSize: int):
        try:
            response = self.serial_read(readSize)
            # logger.debug("Received: [{}]".format(str(response, 'utf-8')))
            return response
        except serial.SerialTimeoutException:
            # We timed-out trying to read from our device, slow things down.
            logger.warning("(Read data) Too fast! Slow down!")
        except serial.SerialException:
            # Error writing data to device: close and reopen serial port, try to read again
            logger.error(
                "SerialException: Failed to read serial data from device. Closing and reopening COM port before retrying once.")
            self.closeSerial()
            time.sleep(1)
            self.openSerial()
            return self.serial_read(readSize)

    @staticmethod
    def auto_detect_com_port() -> Optional[str]:
        # To implement only for screens that use serial commands
        pass

    @abstractmethod
    def InitializeComm(self):
        pass

    @abstractmethod
    def Reset(self):
        pass

    @abstractmethod
    def Clear(self):
        pass

    @abstractmethod
    def ScreenOff(self):
        pass

    @abstractmethod
    def ScreenOn(self):
        pass

    @abstractmethod
    def SetBrightness(self, level: int):
        pass

    def SetBackplateLedColor(self, led_color: Tuple[int, int, int] = (255, 255, 255)):
        pass

    @abstractmethod
    def SetOrientation(self, orientation: Orientation):
        pass

    @abstractmethod
    def DisplayPILImage(
            self,
            image: Image.Image,
            x: int = 0, y: int = 0,
            image_width: int = 0,
            image_height: int = 0
    ):
        pass

    def DisplayBitmap(self, bitmap_path: str, x: int = 0, y: int = 0, width: int = 0, height: int = 0):
        image = self.open_image(bitmap_path)

        # Resize the picture if custom width/height provided
        if width != 0 and height != 0:
            if width != image.size[0] or height != image.size[1]:
                image = image.resize((width, height))

        self.DisplayPILImage(image, x, y, width, height)

    def DisplayText(
            self,
            text: str,
            x: int = 0,
            y: int = 0,
            width: int = 0,
            height: int = 0,
            font: str = "./res/fonts/roboto-mono/RobotoMono-Regular.ttf",
            font_size: int = 20,
            font_color: Color = (0, 0, 0),
            background_color: Color = (255, 255, 255),
            background_image: Optional[str] = None,
            align: str = 'left',
            anchor: str = 'la',
    ):
        # Convert text to bitmap using PIL and display it
        # Provide the background image path to display text with transparent background

        font_color = parse_color(font_color)
        background_color = parse_color(background_color)

        assert x <= self.get_width(), 'Text "' + text + '" X coordinate ' + str(x) + ' must be <= display width ' + str(
            self.get_width())
        assert y <= self.get_height(), 'Text "' + text + '" Y coordinate ' + str(y) + ' must be <= display height ' + str(
            self.get_height())
        assert len(text) > 0, 'Text must not be empty'
        assert font_size > 0, "Font size must be > 0"

        # If only width is specified, assume height based on font size (one-line text)
        if width > 0 and height == 0:
            height = font_size

        if background_image is None:
            # A text bitmap is created with max width/height by default : text with solid background
            text_image = Image.new(
                'RGB',
                (self.get_width(), self.get_height()),
                background_color
            )
        else:
            # The text bitmap is created from provided background image : text with transparent background
            text_image = self.open_image(background_image)

        # Get text bounding box
        ttfont = self.open_font(font, font_size)
        d = ImageDraw.Draw(text_image)

        if width == 0 or height == 0:
            left, top, right, bottom = d.textbbox((x, y), text, font=ttfont, align=align, anchor=anchor)

            # textbbox may return float values, which is not good for the bitmap operations below.
            # Let's extend the bounding box to the next whole pixel in all directions
            left, top = math.floor(left), math.floor(top)
            right, bottom = math.ceil(right), math.ceil(bottom)
        else:
            left, top, right, bottom = x, y, x + width, y + height

            if anchor.startswith("m"):
                x = int((right + left) / 2)
            elif anchor.startswith("r"):
                x = right
            else:
                x = left

            if anchor.endswith("m"):
                y = int((bottom + top) / 2)
            elif anchor.endswith("b"):
                y = bottom
            else:
                y = top

        # Draw text onto the background image with specified color & font
        d.text((x, y), text, font=ttfont, fill=font_color, align=align, anchor=anchor)

        # Restrict the dimensions if they overflow the display size
        left = max(left, 0)
        top = max(top, 0)
        right = min(right, self.get_width())
        bottom = min(bottom, self.get_height())

        # Crop text bitmap to keep only the text
        text_image = text_image.crop(box=(left, top, right, bottom))

        self.DisplayPILImage(text_image, left, top)

    def DisplayProgressBar(self, x: int, y: int, width: int, height: int, min_value: int = 0, max_value: int = 100,
                           value: int = 50,
                           bar_color: Color = (0, 0, 0),
                           bar_outline: bool = True,
                           background_color: Color = (255, 255, 255),
                           background_image: Optional[str] = None,
                           reverse_direction: Optional[bool] = False):
        # Generate a progress bar and display it
        # Provide the background image path to display progress bar with transparent background

        bar_color = parse_color(bar_color)
        background_color = parse_color(background_color)

        assert x <= self.get_width(), 'Progress bar X coordinate must be <= display width'
        assert y <= self.get_height(), 'Progress bar Y coordinate must be <= display height'
        assert x + width <= self.get_width(), 'Progress bar width exceeds display width'
        assert y + height <= self.get_height(), 'Progress bar height exceeds display height'

        # Don't let the set value exceed our min or max value, this is bad :)
        if value < min_value:
            value = min_value
        elif max_value < value:
            value = max_value

        assert min_value <= value <= max_value, 'Progress bar value shall be between min and max'

        if background_image is None:
            # A bitmap is created with solid background
            bar_image = Image.new('RGB', (width, height), background_color)
        else:
            # A bitmap is created from provided background image
            bar_image = self.open_image(background_image)

            # Crop bitmap to keep only the progress bar background
            bar_image = bar_image.crop(box=(x, y, x + width, y + height))

        # Draw progress bar. Fill has to be computed from the offset
        # into [min_value, max_value], not the raw value; otherwise a
        # bar with min_value > 0 (e.g. a 25..95 temperature bar) is
        # filled by the wrong fraction.
        if width > height:
            bar_filled_width = ((value - min_value) / (max_value - min_value) * width) - 1
            if bar_filled_width < 0:
                bar_filled_width = 0
        else:
            bar_filled_height = ((value - min_value) / (max_value - min_value) * height) - 1
            if bar_filled_height < 0:
                bar_filled_height = 0
        draw = ImageDraw.Draw(bar_image)

        # most common setting
        x1 = 0
        y1 = 0
        x2 = width - 1
        y2 = height - 1

        if width > height:
            if reverse_direction is True:
                x1 = width - 1 - bar_filled_width
            else:
                x2 = bar_filled_width
        else:
            if reverse_direction is True:
                y2 = bar_filled_height
            else:
                y1 = height - 1 - bar_filled_height
        draw.rectangle([x1, y1, x2, y2], fill=bar_color, outline=bar_color)

        if bar_outline:
            # Draw outline
            draw.rectangle([0, 0, width - 1, height - 1], fill=None, outline=bar_color)

        self.DisplayPILImage(bar_image, x, y)

    # Load image from the filesystem, or get from the cache if it has already been loaded previously
    def open_image(self, bitmap_path: str) -> Image.Image:
        if bitmap_path not in self.image_cache:
            logger.debug("Bitmap " + bitmap_path + " is now loaded in the cache")
            self.image_cache[bitmap_path] = Image.open(bitmap_path)
        return copy.copy(self.image_cache[bitmap_path])

    def open_font(self, name: str, size: int) -> ImageFont.FreeTypeFont:
        if (name, size) not in self.font_cache:
            self.font_cache[(name, size)] = ImageFont.truetype(name, size)
        return self.font_cache[(name, size)]
