import sys
import os
import re
import datetime
import functools
import time
import asyncio
from typing import *
from pathlib import Path
from dataclasses import dataclass
import mido


print("Auto-imported for your convenience:")
print(
    ", ".join(
        (
            "asyncio",
            "dataclasses.dataclass",
            "datetime",
            "functools",
            "os",
            "pathlib.Path",
            "re",
            "sys",
            "time",
            "typing.*",
            "mido",
        )
    )
)

os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = "/opt/homebrew/lib/"

import threading
import queue
from collections import deque


LAUNCHPAD_PORT = "Launchpad Pro Standalone Port"
CLOCK_PORT = "IAC aiotone"


SysEx = functools.partial(mido.Message, "sysex")
portmidi = mido.Backend("mido.backends.portmidi", load=True)


class MIDIOut(threading.Thread):
    def __init__(self, port: str) -> None:
        super().__init__(name=f"MIDI Out [{port}]", daemon=True)
        self.queue: queue.Queue[mido.Message] = queue.Queue()
        self.connected = False
        self.port = port
        self.start()

    def send(self, message: mido.Message) -> None:
        self.queue.put(message, block=False)

    def run(self):
        while self.port not in portmidi.get_output_names():
            time.sleep(1)
        output = portmidi.open_output(self.port)
        self.connected = True

        while True:
            message = self.queue.get()
            output.send(message)


class Stopped(Exception):
    pass


class Board(threading.Thread):
    def __init__(self):
        super().__init__(name="Board", daemon=True)

        self.connected = False
        self.coords = bytearray(b"\x00\x20\x29\x02\x10\x0A" + (b"\x00" * 130))
        for pad_index in range(64):
            pad_coord = self.index_to_pad(pad_index)
            self.coords[6 + 2 * pad_index] = pad_coord
        self.coords[6 + 128] = 0x63  # side LED
        self.reset()

        while LAUNCHPAD_PORT not in portmidi.get_output_names():
            time.sleep(1)
        self.output = portmidi.open_output(LAUNCHPAD_PORT)
        # not connected yet, wait for input in run()

    def run(self):
        while LAUNCHPAD_PORT not in portmidi.get_input_names():
            time.sleep(1)

        counter = -1
        input = portmidi.open_input(LAUNCHPAD_PORT)
        self.connected = True
        while True:
            counter = (counter + 1) % 100
            if not counter:
                self.maybe_update()

            message = input.poll()
            if not message:
                time.sleep(0.0001)
                continue
            if message.type == "note_off":
                message.type = "note_on"
                message.velocity = 0

            if message.type == "note_on":
                index = self.pad_to_index(message.note) + 1
                if message.velocity:
                    self.pad(index, 0x03)
                else:
                    self.pad(index, 0x00)

    def clock_update(self):
        self.self_update = False
        self.update()

    def maybe_update(self):
        if self.self_update:
            self.update()

    def update(self):
        self.output.send(SysEx(data=self.coords))

    def reset(self):
        self.self_update = True
        self.coords[6 + 129] = 0x01  # side LED

    def pad(self, number: int, color: int) -> None:
        """`number` is 1-indexed. Color is Launchpad programmer's mode."""
        self.coords[6 + 2 * (number - 1) + 1] = color

    def index_to_pad(self, index: int) -> int:
        """Argument is 0-indexed 8x8 grid coord. Result is Launchpad NOTE_ON note number."""
        x = index % 8
        y = index // 8
        return 10 * y + x + 11

    def pad_to_index(self, pad: int) -> int:
        """Argument is Launchpad NOTE_ON note number. Result is 0-indexed 8x8 grid coord."""
        x = (pad - 1) % 10
        y = (pad - 1) // 10
        return 8 * (y - 1) + x


class Clock(threading.Thread):
    def __init__(self):
        super().__init__(name="Clock", daemon=True)
        self.connected = False
        self.on_beat = threading.Event()
        self.on_bar = threading.Event()
        self.countdown_lock = threading.Lock()
        self.countdowns = deque()
        self.board = Board()
        self.board.start()
        self.reset()

    def reset(self):
        self.running = False
        self.on_beat.clear()
        self.on_bar.clear()
        self.position = -1  # pulses since last start
        self.beat = -1
        self.bar = -1
        self.board.reset()
        with self.countdown_lock:
            for countdown in self.countdowns:
                countdown.set()
            self.countdowns = deque(threading.Event() for _ in range(384))

    def run(self):
        while CLOCK_PORT not in portmidi.get_input_names():
            time.sleep(1)
        input = portmidi.open_input(CLOCK_PORT)

        self.connected = True

        for message in input:
            if message.type == "clock":
                self.board.clock_update()
                if self.running:
                    self.tick()
            elif message.type in {"start", "continue"}:
                self.reset()
                self.running = True
            elif message.type == "stop":
                self.reset()

    def tick(self) -> None:
        self.position = (self.position + 1) % 24
        if self.position == 0:
            self.on_beat.set()
            self.beat = (self.beat + 1) % 4
            if self.beat == 0:
                self.on_bar.set()
                self.bar = (self.bar + 1) % 4
                self.board.pad(65, 0x34)
            else:
                self.board.pad(65, 0x35)
        else:
            if self.position == 12:
                self.board.pad(65, 0x36)
            self.on_beat.clear()
            self.on_bar.clear()
        with self.countdown_lock:
            current_ev = self.countdowns.popleft()
            current_ev.set()
            self.countdowns.append(threading.Event())

    def wait(self, pulses: int) -> None:
        if pulses == 0:
            return

        with self.countdown_lock:
            countdown = self.countdowns[pulses - 1]
        countdown.wait()
        if not self.running:
            raise Stopped("Clock stopped while waiting")


clock = Clock()
board = clock.board


def method_on(cls_or_obj):
    def attach_method(meth):
        if not isinstance(cls_or_obj, type):
            meth = meth.__get__(cls_or_obj, cls_or_obj.__class__)
        setattr(cls_or_obj, meth.__name__, meth)

    return attach_method


class Seq(threading.Thread):
    def __init__(self, number):
        super().__init__(name=f"Sequence {number}", daemon=True)
        self.number = number
        self.start()

    def play(self):
        for _ in range(4):
            clock.board.pad(self.number, 0x03)
            clock.wait(6)
            clock.board.pad(self.number, 0x40)
            clock.wait(6)

    def run(self):
        while True:
            clock.board.pad(self.number, 0x40 if clock.running else 0x01)
            try:
                clock.on_bar.wait()
            except Stopped:
                continue

            try:
                self.play()
            except Stopped:
                continue


s = []
for i in range(1, 65):
    s.append(Seq(i))
