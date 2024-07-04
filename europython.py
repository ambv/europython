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


class Clock(threading.Thread):
    def __init__(self):
        super().__init__(name="Clock", daemon=True)
        self.connected = False
        self.on_beat = threading.Event()
        self.on_bar = threading.Event()
        self.countdown_lock = threading.Lock()
        self.countdowns = deque()
        self.board = bytearray(b"\x00\x20\x29\x02\x10\x0A" + (b"\x00" * 128))
        for pad_index in range(64):
            pad_coord = self.index_to_pad(pad_index + 1)
            self.board[6 + 2 * pad_index] = pad_coord
        self.reset()

    def reset(self):
        self.running = False
        self.on_beat.clear()
        self.on_bar.clear()
        self.position = -1  # pulses since last start
        self.beat = -1
        self.bar = -1
        for pad_index in range(64):
            self.board[6 + 2 * pad_index + 1] = pad_index
        with self.countdown_lock:
            for countdown in self.countdowns:
                countdown.set()
            self.countdowns = deque(threading.Event() for _ in range(384))

    def run(self):
        while CLOCK_PORT not in portmidi.get_input_names():
            time.sleep(1)
        input = portmidi.open_input(CLOCK_PORT)

        while LAUNCHPAD_PORT not in portmidi.get_output_names():
            time.sleep(1)
        launchpad = portmidi.open_output(LAUNCHPAD_PORT)

        self.connected = True

        launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x01"))
        launchpad.send(SysEx(data=self.board))

        for message in input:
            if self.running and message.type == "clock":
                self.tick(launchpad)
            elif message.type in {"start", "continue"}:
                self.reset()
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x36"))
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x23\x63\x35"))
                self.running = True
            elif message.type == "stop":
                self.reset()
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x01"))

    def pad(self, number: int, color: int) -> None:
        self.board[6 + 2 * (number - 1) + 1] = color

    def index_to_pad(self, index: int) -> int:
        x = (index - 1) % 8
        y = (index - 1) // 8
        return 10 * y + x + 11

    def tick(self, launchpad) -> None:
        launchpad.send(SysEx(data=self.board))

        self.position = (self.position + 1) % 24
        if self.position == 0:
            self.on_beat.set()
            self.beat = (self.beat + 1) % 4
            if self.beat == 0:
                self.on_bar.set()
                self.bar = (self.bar + 1) % 4
        else:
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


class Seq(threading.Thread):
    def __init__(self, number):
        super().__init__(name=f"Sequence {number}", daemon=True)
        self.number = number
        self.start()

    def play(self):
        for _ in range(4):
            clock.pad(self.number, 0x03)
            clock.wait(6)
            clock.pad(self.number, 0x40)
            clock.wait(6)

    def run(self):
        clock.pad(self.number, 0x01)

        while True:
            clock.on_bar.wait()
            try:
                self.play()
            except Stopped:
                continue


s = []
for i in range(1, 65):
    s.append(Seq(i))
