"""Hardware-independent conversation lifecycle."""
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


@dataclass
class ConversationState:
    last_user: float
    speaking: bool = False
    generating: bool = False

    def idle(self, now, timeout, playback_busy):
        return not (self.speaking or self.generating or playback_busy) and now - self.last_user >= timeout

