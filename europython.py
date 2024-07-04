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

LAUNCHPAD_PORT = "Launchpad Pro Standalone Port"
CLOCK_PORT = "IAC aiotone"


SysEx = functools.partial(mido.Message, "sysex")


class MIDIOut(threading.Thread):
    def __init__(self, port: str) -> None:
        super().__init__(name=f"MIDI Out [{port}]", daemon=True)
        self.queue: queue.Queue[mido.Message] = queue.Queue()
        self.connected = False
        self.port = port
        self.start()
    
    def send(self, message: mido.Message) -> None:
        self.queue.put_nowait(message)

    def run(self):
        portmidi = mido.Backend("mido.backends.portmidi")
        while self.port not in portmidi.get_output_names():
            time.sleep(1)
        output = portmidi.open_output(self.port)
        self.connected = True

        while True:
            message = self.queue.get()
            output.send(message)


launchpad = MIDIOut(LAUNCHPAD_PORT)


class Stopped(Exception):
    pass


class Clock(threading.Thread):
    def __init__(self):
        super().__init__(name="Clock", daemon=True)
        self.connected = False
        self.on_beat = threading.Event()
        self.on_bar = threading.Event()
        self.countdown_lock = threading.Lock()
        self.countdowns = []
        self.reset()

    def reset(self):
        self.running = False
        self.on_beat.clear()
        self.on_bar.clear()
        self.position = -1  # pulses since last start
        self.beat = -1
        self.bar = -1
        with self.countdown_lock:
            for countdown in self.countdowns:
                countdown.set()
            self.countdowns = []

    def run(self):
        portmidi = mido.Backend("mido.backends.portmidi")
        while CLOCK_PORT not in portmidi.get_input_names():
            time.sleep(1)
        input = portmidi.open_input(CLOCK_PORT)
        self.connected = True

        launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0E\x00"))
        launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x01"))

        for message in input:
            if self.running and message.type == "clock":
                self.tick()
            elif message.type in {"start", "continue"}:
                self.reset()
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x36"))
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x23\x63\x35"))
                self.running = True
            elif message.type == "stop":
                self.reset()
                launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A\x63\x01"))
    
    def tick(self) -> None:
        # If we refresh launchpad here, it will be 1/24 behind but this will enable
        # all sequencers to set the state.

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
            done_indexes = []
            for index, countdown in enumerate(self.countdowns):
                countdown.tick()
                if countdown.is_set():
                    done_indexes.append(index)
            for index in reversed(done_indexes):
                del self.countdowns[index]
        
    def wait(self, pulses: int) -> None:
        if pulses == 0:
            return
        
        countdown = Countdown(pulses)
        with self.countdown_lock:
            self.countdowns.append(countdown)
        countdown.wait()
        if not self.running:
            raise Stopped("Clock stopped while waiting")


class Countdown(threading.Event):
    def __init__(self, value: int) -> None:
        super().__init__()
        self.value = value

    def tick(self) -> None:
        self.value -= 1
        if self.value == 0:
            self.set()


clock = Clock()


class Seq(threading.Thread):
    def __init__(self, clock, number):
        super().__init__(name=f"Sequence {number}", daemon=True)
        self.number = number
        x = (number - 1) % 8
        y = (number - 1) // 8
        self.pad = 10 * y + x + 11
        self.clock = clock
        self.start()

    def play(self):
        for _ in range(4):
            launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A" + self.pad.to_bytes(1) + b"\x03"))
            self.clock.wait(6)
            launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A" + self.pad.to_bytes(1) + b"\x40"))
            self.clock.wait(6)

    def run(self):
        launchpad.send(SysEx(data=b"\x00\x20\x29\x02\x10\x0A" + self.pad.to_bytes(1) + b"\x01"))

        while True:
            self.clock.on_bar.wait()
            try:
                self.play()
            except Stopped:
                continue


s = []
for i in range(1, 65):
    s.append(Seq(clock, i))
