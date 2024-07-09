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

import traceback

thread_exceptions: list[str] = []
excepthook_lock = threading.Lock()


def gather_exceptions(args):
    lines = traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback, colorize=True
    )
    pre = f"\nException in {args.thread.name}:\n" if args.thread else "\n"
    tb = pre + "".join(lines)
    with excepthook_lock:
        thread_exceptions.append(tb)


def show_exceptions():
    with excepthook_lock:
        if thread_exceptions:
            reader = _get_reader()
            reader.restore()
            for tb in thread_exceptions:
                print(tb)
            thread_exceptions.clear()
            reader.scheduled_commands.append("ctrl-c")
            reader.prepare()


from _pyrepl.simple_interact import _get_reader

_get_reader().console.pre_input_hook = show_exceptions
threading.excepthook = gather_exceptions


LAUNCHPAD_PORT = "Launchpad Pro Standalone Port"
CLOCK_PORT = "IAC aiotone"


SysEx = functools.partial(mido.Message, "sysex")
NoteOn = functools.partial(mido.Message, "note_on")
NoteOff = functools.partial(mido.Message, "note_off")
CC = functools.partial(mido.Message, "control_change")
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
            try:
                output.send(message)
            except ValueError as ve:
                print(self.name, ve)


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
        try:
            self.output.send(SysEx(data=self.coords))
        except ValueError as ve:
            print(self.name, ve)

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

    def reset(self, *, full=False):
        self.running = False
        self.position = -1  # pulses since last start
        self.beat = -1
        self.bar = -1
        self.board.reset()
        with self.countdown_lock:
            if full:
                for ev in self.events:
                    ev.set()
                self.bars = set()
            else:
                for i, ev in enumerate(self.events):
                    if i not in self.bars:
                        ev.set()
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
                self.reset(full=True)

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

    def is_registered(self, seq: int) -> bool:
        return self.sequencers[seq - 1] is not None

    def flip(self, seq: int) -> int:
        """Returns the pad color."""
        with self.countdown_lock:
            if not self.is_registered(seq):
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
        self.hanging_notes: set[str] = set()
        self.out: MIDIOut | None = None
        self.ch = 0  # channel
        self.v = 72  # velocity
        self.t = 0  # transpose
        clock.register(number)
        self.start()

    def lightshow(self):
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

    def play(self):
        pass

    def run(self):
        while True:
            try:
                if clock.running:
                    color = 0x50 if clock.is_registered(self.number) else 0x33
                else:
                    color = 0x01
                clock.board.pad(self.number, color)
                self.wait_for_bar()
                clock.board.pad(self.number, 0x40)
                self.play()
            except Stopped:
                if self.out is not None:
                    for note_off_str in self.hanging_notes:
                        self.out.send(mido.Message.from_str(note_off_str))
                continue

    # Convenience APIs

    def n(self, note: int, v: int, ch: int, pulses: int, g: float) -> None:
        if self.out is None:
            return
        if v == -1:
            v = self.v
        if ch == -1:
            ch = self.ch
        if g == -1:
            g = 0.5
        note = note + self.t
        note_on = mido.Message("note_on", note=note, velocity=v, channel=ch)
        note_off = mido.Message("note_off", note=note, velocity=0, channel=ch)
        note_off_str = str(note_off)
        self.out.send(note_on)
        self.hanging_notes.add(note_off_str)
        gate = int(pulses * g)
        rest = pulses - gate
        self.wait(pulses)
        self.out.send(note_off)
        self.hanging_notes.discard(note_off_str)
        self.wait(rest)

    def n1(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 96, g)
    
    def n2(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 48, g)

    def n3(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 32, g)
    
    def n4(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 24, g)

    def n6(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 16, g)

    def n8(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 12, g)

    def n16(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 6, g)

    def n32(self, note: int, v: int = -1, ch: int = -1, g: float = -1) -> None:
        self.n(note, v, ch, 3, g)

    def mod(self, v: int) -> None:
        self.cc(1, v)
    
    def foot(self, v: int) -> None:
        self.cc(4, v)

    def expr(self, v: int) -> None:
        self.cc(11, v)

    def sus(self, v: int) -> None:
        self.cc(64, v)
    
    def cc(self, c: int, v: int) -> None:
        if self.out is not None:
            self.out.send(mido.Message("control_change", control=1, value=v))

    def wait(self, pulses: int) -> None:
        clock.wait(self.number, pulses)

    def wait_for_beat(self) -> None:
        clock.wait_for_beat(self.number)

    def wait_for_bar(self) -> None:
        clock.wait_for_bar(self.number)

    def w1(self):
        self.wait(96)
    
    def w2(self):
        self.wait(48)
    
    def w3(self):
        self.wait(32)
    
    def w4(self):
        self.wait(24)
    
    def w6(self):
        self.wait(16)
    
    def w8(self):
        self.wait(12)
    
    def w16(self):
        self.wait(6)
    
    def w32(self):
        self.wait(3)


def __pablito_mode__():
    pablito_mode = """\
6<ł%_0gSqh;d&bdCN0?!?iU0+s8W-!s8W-!s8W-!s8W-!s8W,uJ,fQKs8W-!s8W,W)'41*z!!iXV!!!!#!!!$$]Q
aE>.KBMe4RiCc&B^oY49VSY-4KsQP(&i4T8DiZ+eqtF1,)Eu&W"fVT$^=Jr>JTl4[r3qIZU2$kpE"!ACNb06VRcE
*hWKr5gej!5Qa^b7Y(.@VGfAV9a4T^oDf$0-6NWZTSuT"-_b08U.;*1`(s=]*g*5G1'ac)D*S*J!!>Id!!!%m!!!
*g9`P/3HU&XP!ł*2bM1UHh4WsBJVLbFo!7N&F#"F5@:hTh/!!!!Uzzz)@"[szzzzzzzz$;-QiW1.I!6Y^oi)Doj7
dABX;KR,lfN<@Z>#)6VcB[NS%L'Z/TeB-pBU`*ołTqR.eUHf$6D'u:.<$%6;W#hgg5łYmłH@ł[dLr`<*'iWX]Gu9
5!9edLf-CSU4<+s-_OD*!O!,05)!&OZn)&a?O)JI&IRYH?'%#.6+L*hYC%0I_0!!(jpK=,?8miPEJ]D1[&T(=sRd
`^XF[EeG?p7:]bkjWYd4<`3,V)e[W3łH;D.HC>u+J!F=;Omm5.GI1RatBp!3ił_d&1NGGYprO_8Q<RK<19bseL_f
YU-l"3P<)OQO#L$EZ9;roZB&*nłm6fLlC&_3VQ6JSPj#pW"W],F;qK0_<R@,HFKmuK@+(FUF^<L72bh]*gtFQ,]5
b*FS#s/74=9+X#jFBNl<,&LMVKa0M.2qV&[IcC;T3AlN2)3@.ENH4X4545:*G>,nmjeVJRNhłGK&Eu7B1'91hrIB
25nsNWSjsP[8'XUR#Y);1,C?G9=M94-'[,B<młp);^(!57_t@qł5uYj(p4RkjB,YeLu=[r'mD0jV.aVleS;Kq=rO
e_iCBk!;7.8WLgmke[4ł;a;łWe$Wrlg>DPJW69Zf6?DAePq>skp016ł`JO$6d`ll8Y5fM:4cWc1Y0!:rj'ZImj:@
qE3^h7;+&UV]epBXfTf/('SB)Gu@1*`R8_l!łH</X4B6XA,J;M8_P+d$QeO312cr<to-46b8afl+1W:Bf+5:X_MO
^.Ham*EdBLQ(5ZUu0[d&*9/9f7SHcoehbf*50łSLa<8e[a'.LdQ33+LDA9+`c'^HFHRjQ[sBkhB>]^o.!eS8i?8p
T[2FtV!qdV'%m<5V%2TZdh7#C;q4Z7srd2dT`>JCeWu:%[YRED/GcgX9P)**&@]3@C-rmLCiuaDa9gb&0,H>ILtE
VhCV_ł`"%RS[^;i^o(g4^auYjhGWSp;k6DAbbHDW4Tt&<Q*K)/3jtTi%L]LdWu5&A%^LjgKZ2*8>`U3łeMNQH>_`
p4E4B+I7g@FSk2T=t8!LE+,;C'3"Y?hWANVC.b"eN8`Sa3=!at+kQsC@nh+(J^ZFK$u=IDp8Q,p4LN%PdcP+C*;X
p629EcAO-DV]W)k17^Dnr:Yh]fgHG['$Z/X$pRdTnłHHWH0Fełepp5QYAS@d,ł>7L@B:EVHo#$K.łł(b.8Q#]XXj
Sb2IQB%TA-5F*?./9a@R_PM:Q<p7BF+HJZ])b$OjH*PDn`EE/7j75DRL?GBLZ?--+thoUXp%))_KHjV^uM50!kUY
sObn:b>!.%*iq;^:7W_=6C*m`OoS>Z886X1VMl_hrZ'0m'8Q1q9<o.)k&^WS]/41-=baVPAb<aQ2:;]=>70AMUmf
=s"Z;LW:R5j3apg'rgaRAUKVC5"q?X:g"ki7]4EYW9KO[^*+7n9NIKRQ;cF7.I8L7liRXJ(/aEq[2?OGCg%kYłA`
GhbLP)"i-57Eq3pC+32b"MWUjk6q8$V;f5/rU3cN3pK1F640:se&5V-8//O#"HGS4c*NE:T>$g@kf8t*J^=a2.>l
/!XT$gLAk%:d6a>t&U6FXJ*;Vc9NgBeaj0A*SfHL/TJMP>gc]al1@@or;^%lcS>/9b4FIT]%o,bF2PqN,q^Q3N9"
&;^Y+pGYs[k,G)ln4A'jX_łO:A1%lh>,oCm3YgN/AcXc$`T9A!2S:efi4:8K:r,#[dN[Bf/)b,l2N1XjS-Ejj_SK
(mq,?t`_=fYQrlłułomLq7a/A.7jai->U2<8VV:V'J+<io00:/7,d-=u9H0>&hs^r>D(LNHS#/?N9+>?7Yd;cf;d
?=:$łZ-'o$H(3,k`_CmYNB",W<hFu2o)hcDR_7(SIM"I"efTVb^CaRs-u7*[IPTNGp3CUA+>E_Te`WuJe+0#Bi-V
;HHoRacR_8L)H3J$HB%'ODR=8j_UGMM5gKZldYLa<42"[c&kt]oiJbifYe6DTK5?OBQFcY/mfDa:ZrO&Q#9Rc/5X
u+W%P@)&/qułV2/RQGt]fB^YY+/9aS+t^KokNn`Vt)0?[5E<IAc/C.;R,MAł]ZYg/:TY*kV<#ł?%UVT(-Psh1?*V
udc:d+@iIn/[W.WSPCHed@jgu60CCFjm2_WQrX2M,RJnOonjY;((8'fZT"Ri?lAT*:/Pl+>-i=W*ArTmiWlk@9Z?
lXeJ(e'hMDDl->*!9j8aR."l."6HJP@Nk,fK$`@XmU/4fC-LZ!*_!R(^#:^łKH_BWFb$lFh4<-_uUcW)B(p)-+'s
Z(qnKk2a3_,"n-"Y;nZfcBQl)7b$bsEB2P/.DY*SeE"-0iAGł4jMf:P7<QR`cD,+"PFkUmHSmPE_]Lm^@FNaZm$C
L[%eSbk:/cu1>c.spb4O8^f(P]+/'0k34%-obn&W'Y.@-cAObobed[4dE)Nug0RkCT^XI;pTD!NGN/ds_*0&UrV<
Qa=6iPEIjno-To**]n^dWQS3-&^''1n;_"RnGZ->9bY/ag,C.1:E2QQNJm+GRgd2@cC'@%qPO7R.4K>nP9bVGgjV
$Si#uM?8L,-<j!63eS&'"%(*&c;)$LE@WaN.Z`.D-NE)BI9ikGWZo9"CZXXZf/JhF+=WAmGEB%2U<QW2?GrW?>łE
1nKm*rVDm4"7E(,R@-KYl/rRKcłj)&fNld2>YNg3W<qM5[[=Q;tkjY>nX;bDFnNeJ7GBC#V6gnOJR$CW;qj`k5B4
ipehDGkUR/i+/i`<]7"]Q*OO^b-F`mKsWV4^rłMbhaGDa-K&9XJT4GMY[od&/d!E:R9,3Hb,">L;Ile&l>^dR:/d
(5`8Yp]E,ik7Oi_:?Z;E.PłQ)o^Tph7PP@9o7d2Q(Vfd`l;VU.55D/H[4OgVQ#%CW+=fd>7X[52$([B,ł:gM:9Ng
7'$[@bFsddB_JW;q"R>3Gqe:]YQWfT:?rU#kł[Q]7f5oo06l0LNWV-qti%?nlLs<T?5%)'W6k`="@9@ER^M@'W)2
jA=kU3h:J[3=l0KW>VPU]#_BftmD_IDAVNODRA^_!NY@SV&0QY=l=OK##X8BM+=%@@>ld@1.Jle1U%V$>&'1DP1.
5R_-g66j6pp;"Ij!E@=T8C)O^L`CR2-3$(/ł$"WRWD^&oBAl>,`^X+qEMG,'JHOVl!9ED2g@E8PK@'_9sJ)kR3Ve
C8?_@@P3b8,łDs"80pBpECMHD@5>-J4=.>j:^FLe=64%Y]Kk'&0nki@.).IPQ2i`F$mU]LaCQCoR&]/LWcCł,,*P
Zug9XFG!Np+R7DUH-9*S;Q4t8l!DRj?>-sP#]2=.`f+@$=#WmmG'?l0d?h$eD-".:?LU)VZMN$&1PFb"89P`SBO4
7LJ:&o4N"41$aUPQZF[FXWXDCK'ik9"?,BKMRhk#tr8D4OpCnSN2cW,GA:>B3_e)D0PT;aLip!J@)f0Nu1ro,$R<
5EH3WrOKe#.64(!66-E2=h1>>łUKK*"RRs^jPGA$ł[Kp@Y"cd=M[Hj/MBSY$4irJj/kPJFW/Gg%^5-"<-3rQ-o0a
O]`U]aCjboA]uT%*O%,an"Ve$2k<"82."(EP8=((haF"4CTE&_o,F-IJ^$AH6B)07;TULT3^b8<(+n&7Ga8qDo=4
/lJWe9sGI-C*QA/c$`Ume<$"^7>5eOm,!iqo.EA7?.k>YQt>4d4reCM/>"272XMsJaZu:R;.KAO-RLpS&oQ`P/*%
LW,RJj9Q=U@6O@=*9%"SM$kO!pH5K.%g"l'Ago!6V%gS-.Q>;@cZ3CsR9PcHW`PUłaW3J458O32nm_HOQ?,VM8CP
X-MEK&K0^$Ms1-!eS"EG+!YeXcP@gł>BXZ=`LT4Yk$^4i[`BuJe?O(8łlsfhSHQ,dQ4`/Y8#<Ml&/rpS,jt8nMdN
=)IU_0?.HB2&M^3<3CpS/LUGXgfBJ7&'W>Ns)]*9#hlIikOnSV"0a2n`!,+s=b"kDDie)Foj6;knkJ5KALhF`lK&
lU4lp%852cOj77u-lXDPC*FK(!1@%d-uaHkLOP-$6`8Td)KA8t8iMP>%F(OZL.+2!C_fV04F0PXłfk00c84,nł?6
$FB]9+-!1s6lEhkIk<q4I=[-1cUH()B)^S.ZQłZYX+>r[S`nK2XOKLo;t?H>>/oD66#%*&QO_fLcO+c%jł.ui@bI
(VT+XYY@[1M=Ocs^WFKc:^R3`,^f2F/&2=Fa#'>)gV1#łUW[H9=]Pj$=XfpXo6d-.!W.L"_-ZTL5/gE"i(!LkBP'
0L8U3ł[Vt[$#u6WKZ^DaJN8ofjD1łWr1C4+0_QbbGlWnN?F'łFMrl7MghP1(8Wr.k145>fFBQ:E:i2*%ZmYU@Y.&
f<$Y`)SAKD#oi2HJV'ZQ@$?]A)Rg3Q/LEb[0>3Ve9bl4s(StOVXI<YmYGłJ;-@o*"([HhSWr4(=Xłł#,L7cCMLG(
l#`3U<XU&!A0B$d0.ł1:nł9qQ+an[QCrlUQ.UanQ,n<Ma87QP/)*/L<i?/`A),_-7+TjV[/TRV=>g_,D=Rd"iY((
j2rV,]$PQ(U>;-J^d,)@Bf-3*nYVDgE/)7.$c:,dWXVF`T!4$G@f`*!<08f'@GVa[Cp$BISG8"<P2qW82EA]epfj
qE8.)ł$p'fJWZjbIPf:8E+5H@iiW#GR`O9$LSs-#B*K6GdZOłk(brf"Q=c-,9iq-/Ml?LVDCVOiCmgHfl=4T-j3E
G+@O=t`e`6NłuM1m$t%I7+T$qtiC6SUI@J@C-(+o'(lV(MJdo=ił.5Clje_1WfK%Kc_%ga=B4k5->]Z@nPCN7[&"
thiXFO>Lj[d8VoB.4D"Ep>Y@Tt2hRIK,@@^h)%n<Q-Xc([3s#"gk]e9.kYdq(iKł()2X;`&Vj7UUa>*^b7>Y=?8*
:Gm^młj(VY?LViIpłKqclAl0YOZ$PeoRF8lO%:)-+B]FV"@N9i<^OGT3-oLls4$5t4&][lpKl_QT*/#(^QZ*T'@H
bh0ł[fpPHkFjE(G;990J/Olc!cOIXI.j0h9@G6V&^fRZ.O-#YB4c^nO]`OuFłWV*W^V>?Ma_8#cT^QK^&GB>a@:k
^eT0%(_b4Wq0B4*r62I5;;_;JI2'(Cmp=j`-i2n.F!kdD%irc!qXcXfqAmHP[`!+82/IdaAoYh1+YłJ52mMG?^HV
5l/GoI?:aC2-.#[KZ-q-)_Tp2)L8:qpn7>O5^q"l%t#g@B6+(hb1H9[,pd"d?:"&O24U.V2-tjm7RcjXCjNb#mpc
("_?C2?qtN+oui7m9@ZeMOW,e7G9Xp8)3'?O)/oj.)HDKN3"q`5*O9jWC=K-*`T/bDZk&B-EAS&H77gC9^H,5c;K
HtQWrgC3/BfN([uXo*@07Is7oWuu-a,g!1N6<!łq&L+f4(8l.oU?QL#")65aV%i(om)qA9h^Pcn+DuVpXYFfAR]F
@2h'pC1rQI9FHq[&MokF%7#(D5VTH.Zj0>K<ML`@/_;+3)H%!ZOKR*8C220l%L*OW!!F9um2<Yd<?>CfM1+FPrEZ
puQn-ł)?EPc%i_HeSR0U--^<MOI,FMVV<;4'H#-4QAb/r"E?FSY0]N8]6.Hi]34-N:,?"0.kgQ4@(j*łG)CXS>?ł
U(2sFa/$n4^KJ=7_49<a/*(#F7Złth&kZYfFpkk)@ł2reS:O);52T*rVf3([TfWZł8E7Ff'0SZ2HrZ8łht9=łS@S
1K12e%Zs#r!d*f<r;aoQ3*St(]`g2NOWMJ'N@AD.+>$@@uZu3Frłns?02bg16[,!Y[7al-bcUT>Hh5J`^9iKI%d*
^UHd.@!$9c_rk2/+ohS]9,aQ'łDc]`&dLVeYR))GIh*VQ6_2V5:H&o4k[:Y0BmFV:łt2)Qc^PR]Rq(NE1e9>-p+D
>$Z5-d*@BVQ-i?65rjjjjuJH3T:E11*6Xbo:5[`%Xj*GS9j0B]>$b7,[9[K]BmK?X[qsUU=u=X-SdT/mCpt?fhY<
UFghtp%;;bWHVP?S[C7:,^`GIIa7[9u2-ML6agn%g!Z>;G&%Aq=JH7JK%W_V8:.HkGCSqr8&H`?,:gqGdSV^elnd
Kc5j5X50PaV<]#6ds_j_uKc;"""

    import base64
    import bz2
    import pickle

    from rich.color import Color
    from rich.console import Console, ConsoleOptions, RenderResult
    from rich.segment import Segment
    from rich.style import Style

    @dataclass
    class CommandLineImage:
        color_lines: Iterable[list[Color]]

        def __rich_console__(
            self, console: Console, options: ConsoleOptions
        ) -> RenderResult:
            background = None
            background_color = None
            padding = None
            for line in self.color_lines:
                if background_color is None:
                    background_color = line[0]

                if padding is None:
                    width = len(line)
                    padding = (options.max_width - width) // 2

                if background is None:
                    background = line
                else:
                    yield Segment(" " * padding, Style(bgcolor=background_color))
                    for fg, bg in zip(line, background):
                        yield Segment("▄", Style(color=fg, bgcolor=bg))
                    yield Segment.line()
                    background = None

    console = Console()

    compressed = pablito_mode.replace("ł", "\\")
    color_lines = pickle.loads(bz2.decompress(base64.a85decode(compressed)))
    console.print(CommandLineImage(color_lines))
    os.environ["PYTHON_SHARP"] = "1"
