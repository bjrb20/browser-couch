"""Thread-safe player ownership and expiring full-state input snapshots."""
import threading
import time

CENTRE = (0, 0, 0, 0)


class InputHub:
    def __init__(self, players=4, timeout=.6):
        self.players = players
        self.timeout = timeout
        self.lock = threading.Lock()
        self.owners = [None] * players
        self.inputs = {}
        self.sequences = {}
        self.applied = {}
        self.last_polled = [0] * players
        self.polls = [0] * players

    def claim(self, client, slot):
        if type(slot) is not int or not 0 <= slot < self.players:
            raise ValueError('Choose a valid player slot')
        with self.lock:
            if self.owners[slot] not in (None, client):
                raise ValueError('That player slot is already taken')
            for n, owner in enumerate(self.owners):
                if owner == client:
                    self.owners[n] = None
            self.owners[slot] = client
            self.inputs.pop(client, None)
            self.applied.pop(client, None)

    def slot_of(self, client):
        with self.lock:
            return self.owners.index(client) if client is not None and client in self.owners else None

    def seat(self, client, slot):
        """An admin's move: put `client` in `slot` (None: no slot), leaving any other slot it had.

        The slot must be free or already theirs. Held buttons are dropped, so nothing carries over.
        """
        if slot is not None and (type(slot) is not int or not 0 <= slot < self.players):
            raise ValueError('Choose a valid player slot')
        with self.lock:
            if slot is not None and self.owners[slot] not in (None, client):
                raise ValueError('That player slot is already taken')
            self.owners = [None if owner == client else owner for owner in self.owners]
            if slot is not None:
                self.owners[slot] = client
            self.inputs.pop(client, None)
            self.applied.pop(client, None)

    def swap(self, a, b):
        """An admin's swap of two slots' owners; either may be empty."""
        if any(type(n) is not int or not 0 <= n < self.players for n in (a, b)) or a == b:
            raise ValueError('Choose two different player slots')
        with self.lock:
            self.owners[a], self.owners[b] = self.owners[b], self.owners[a]
            for owner in (self.owners[a], self.owners[b]):
                self.inputs.pop(owner, None)
                self.applied.pop(owner, None)

    def resize(self, players):
        """Change the slot count for the next game. Players in slots that still exist keep them."""
        with self.lock:
            for owner in self.owners[players:]:
                self.inputs.pop(owner, None)
                self.applied.pop(owner, None)
            keep = min(players, self.players)
            self.owners = self.owners[:keep] + [None]*(players-keep)
            self.last_polled = self.last_polled[:keep] + [0]*(players-keep)
            self.polls = self.polls[:keep] + [0]*(players-keep)
            self.players = players

    def release(self, client):
        with self.lock:
            self.owners = [None if owner == client else owner for owner in self.owners]
            self.inputs.pop(client, None)
            self.applied.pop(client, None)

    def clear(self, client):
        with self.lock:
            self.inputs.pop(client, None)
            self.applied.pop(client, None)

    def forget(self, client):
        self.release(client)
        with self.lock:
            self.sequences.pop(client, None)

    def update(self, client, seq, mask, axes=None):
        """Store a full input state: button mask and optional [lx, ly, rx, ry] sticks."""
        if type(seq) is not int or not 0 <= seq < 2**53:
            return False
        if type(mask) is not int or not 0 <= mask <= 65535:
            return False
        if axes is None:
            axes = CENTRE
        elif type(axes) is not list or len(axes) != 4 or \
                any(type(a) is not int or not -32768 <= a <= 32767 for a in axes):
            return False
        with self.lock:
            if client not in self.owners:
                return False
            if seq <= self.sequences.get(client, -1):
                return False
            self.sequences[client] = seq
            self.inputs[client] = (seq, mask, time.monotonic(), tuple(axes))
            return True

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            for owner in self.owners:
                entry = self.inputs.get(owner)
                if entry and (not self.applied.get(owner) or self.applied[owner][0] != entry[0]):
                    self.applied[owner] = (entry[0], (now-entry[2])*1000)
            return [entry[1] if (entry := self.inputs.get(owner))
                    and now - entry[2] <= self.timeout else 0
                    for owner in self.owners]

    def analog(self):
        """Stick positions per slot, centred when the input is stale or the slot is empty."""
        now = time.monotonic()
        with self.lock:
            return [entry[3] if (entry := self.inputs.get(owner))
                    and now - entry[2] <= self.timeout else CENTRE
                    for owner in self.owners]

    def timing(self, client):
        with self.lock:
            entry = self.applied.get(client)
            return entry[1] if entry else None

    def polled(self, port, mask):
        with self.lock:
            if 0 <= port < self.players:
                self.last_polled[port] = mask
                if mask:
                    self.polls[port] += 1

    def slots(self):
        with self.lock:
            return list(self.owners)

