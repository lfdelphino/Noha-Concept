"""Hardware-independent conversation lifecycle."""
from collections import deque
from dataclasses import dataclass


@dataclass
class MotionGate:
    boot: float
    warmup: float = 60
    quiet: float = 30
    cooldown: float = 120
    armed: bool = True
    last_greeting: float = float("-inf")
    low_since: float | None = None

    def update(self, now, detected, busy=False):
        if detected:
            self.low_since = None
        elif self.low_since is None:
            self.low_since = now
        if not self.armed and not busy and self.low_since is not None:
            if now - self.low_since >= self.quiet and now - self.last_greeting >= self.cooldown:
                self.armed = True
        if now - self.boot < self.warmup or busy or not self.armed or not detected:
            return False
        self.armed = False
        self.last_greeting = now
        return True

    def readiness(self, now, busy=False):
        """(state, seconds left) for the dashboard: aquecendo, conversando, rearmando or pronto."""
        if now - self.boot < self.warmup:
            return "aquecendo", self.warmup - (now - self.boot)
        if busy:
            return "conversando", 0
        if not self.armed:
            # Re-arms after `quiet` seconds without motion and `cooldown` since the last greeting.
            quiet = self.quiet - (now - self.low_since) if self.low_since is not None else self.quiet
            return "rearmando", max(0, quiet, self.cooldown - (now - self.last_greeting))
        return "pronto", 0


class MotionLog:
    """Wall-clock motion intervals of the last `window` seconds, for the dashboard timeline."""
    def __init__(self, window=600):
        self.window = window
        self.intervals = deque(maxlen=200)  # a flapping sensor must not grow the heartbeat
        self.last = None

    def update(self, wall, detected):
        """Record the sample; True when motion started or stopped."""
        moving = bool(self.intervals) and self.intervals[-1][1] is None
        if detected:
            self.last = wall
        if detected != moving:
            if detected:
                self.intervals.append([wall, None])
            else:
                self.intervals[-1][1] = wall
        while self.intervals and self.intervals[0][1] is not None and self.intervals[0][1] < wall - self.window:
            self.intervals.popleft()
        return detected != moving

    def snapshot(self):
        return [[round(start, 1), end if end is None else round(end, 1)] for start, end in self.intervals]


@dataclass
class ConversationState:
    last_user: float  # last accepted visitor turn or Noha's last word, not every VAD detection
    speaking: bool = False
    generating: bool = False

    def idle(self, now, timeout, playback_busy):
        # VAD "speech" alone must not keep a session open: noise and echo trigger it all night.
        if self.generating or playback_busy:
            self.last_user = now
            return False
        return now - self.last_user >= timeout

