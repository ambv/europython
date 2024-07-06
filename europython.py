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
                    # FIXME: Board shouldn't know about "clock"
                    self.pad(index, clock.flip(index))

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
    def __init__(self) -> None:
        super().__init__(name="Clock", daemon=True)
        self.connected = False
        self.countdown_lock = threading.RLock()
        self.countdowns: deque[set[int]] = deque()
        self.bars: set[int] = set()
        self.beats: set[int] = set()
        self.board = Board()
        self.board.start()
        self.sequencers: list[threading.Event | None] = [None] * 64
        self.events = [threading.Event() for _ in range(64)]
        self.reset()

    def reset(self):
        self.running = False
        self.position = -1  # pulses since last start
        self.beat = -1
        self.bar = -1
        self.board.reset()
        with self.countdown_lock:
            for ev in self.events:
                if ev:
                    ev.set()
            self.bars = set()
            self.beats = set()
            self.countdowns = deque(set() for _ in range(384))

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
            self.beat = (self.beat + 1) % 4
            if self.beat == 0:
                self.bar = (self.bar + 1) % 4
                self.board.pad(65, 0x34)
            else:
                self.board.pad(65, 0x35)
        else:
            if self.position == 12:
                self.board.pad(65, 0x36)
        with self.countdown_lock:
            if self.position == 0:
                if self.beat == 0:
                    for index in sorted(self.bars):
                        ev = self.sequencers[index]
                        if ev is not None:
                            ev.set()
                            self.bars.remove(index)
                for index in sorted(self.beats):
                    ev = self.sequencers[index]
                    if ev is not None:
                        ev.set()
                        self.beats.remove(index)
            current_set = self.countdowns.popleft()
            for index in sorted(current_set):
                ev = self.sequencers[index]
                if ev is not None:
                    ev.set()
                else:
                    self.countdowns[0].add(index)
            self.countdowns.append(set())

    def wait_for_beat(self, seq: int) -> None:
        index = seq - 1
        ev = self.events[index]
        with self.countdown_lock:
            self.beats.add(index)

        self.wait_for(ev, index)

    def wait_for_bar(self, seq: int) -> None:
        index = seq - 1
        ev = self.events[index]
        with self.countdown_lock:
            self.bars.add(index)

        self.wait_for(ev, index)

    def wait(self, seq: int, pulses: int) -> None:
        if pulses == 0:
            return

        index = seq - 1
        ev = self.events[index]
        with self.countdown_lock:
            self.countdowns[pulses - 1].add(index)

        self.wait_for(ev, index)

    def wait_for(self, ev: threading.Event, index: int) -> None:
        ev.wait()
        ev.clear()
        if not self.running:
            raise Stopped("Clock stopped while waiting")
        if self.sequencers[index] is None:
            raise Stopped("Sequencer stopped while waiting")

    def register(self, seq: int) -> None:
        with self.countdown_lock:
            index = seq - 1
            ev = self.events[index]
            ev.clear()
            self.sequencers[index] = ev

    def unregister(self, seq: int) -> None:
        with self.countdown_lock:
            index = seq - 1
            self.events[index].set()
            self.sequencers[index] = None
            self.bars.discard(index)
            self.beats.discard(index)
            for countdown_set in self.countdowns:
                countdown_set.discard(index)

    def flip(self, seq: int) -> int:
        """Returns the pad color."""
        with self.countdown_lock:
            if self.sequencers[seq - 1] is None:
                self.register(seq)
                return 0x30
            else:
                self.unregister(seq)
                return 0x33


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
        clock.register(number)
        self.start()

    def play(self):
        for _ in range(2):
            clock.board.pad(self.number, 0x03)
            self.wait(6)
            clock.board.pad(self.number, 0x40)
            self.wait(6)
        clock.board.pad(self.number, 0x09)
        self.wait(12)
        clock.board.pad(self.number, 0x40)
        self.wait(12)
        clock.board.pad(self.number, 0x06)
        self.wait_for_beat()

    def run(self):
        while True:
            try:
                clock.board.pad(self.number, 0x33 if clock.running else 0x01)
                self.wait_for_bar()
                clock.board.pad(self.number, 0x40)
                self.play()
            except Stopped:
                continue

    def wait(self, pulses: int) -> None:
        clock.wait(self.number, pulses)

    def wait_for_beat(self) -> None:
        clock.wait_for_beat(self.number)

    def wait_for_bar(self) -> None:
        clock.wait_for_bar(self.number)


s = []
for i in range(1, 65):
    s.append(Seq(i))
