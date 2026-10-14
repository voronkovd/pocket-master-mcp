"""A fake Pocket Master that answers SysEx like the real unit (as far as the protocol is known)."""
from __future__ import annotations

import copy
import struct

import mido

from pocket_master_mcp import protocol as P



def default_preset(dm, name="Init"):
    def first(key):
        return dm["models"][key][0]
    st = {"name": name, "codes": {}, "params": {}, "mask": 0b0_1_1001_1111, "chain": [0, 1, 2, 3, 4, 5, 6, 7, 8],
          "volume": 75}
    for b in dm["blocks"]:
        m = first(b["key"])
        st["codes"][b["module"]] = m["code"]
        st["params"][b["module"]] = {p["alg"]: float(p["default"]) for p in m["params"]}
    c = dm["models"]["clone"][0]
    st["codes"][9] = c["code"]
    st["params"][9] = {p["alg"]: float(p["default"]) for p in c["params"]}
    return st


class FakePocketMaster:
    def __init__(self, dev, dm):
        self.dev, self.dm = dev, dm
        self.sent: list[P.Frame] = []
        self.slots = [default_preset(dm, f"Slot {i + 1}" if i not in (0, 9) else "") for i in range(P.PRESET_COUNT)]
        self.current = 3
        self.edit = copy.deepcopy(self.slots[self.current])
        self.drop: list[set[int]] = []   # per multi-packet reply: packet indexes to lose (like flaky USB MIDI)

    # ---- helpers
    def reply(self, payload, packets=1):
        payload = list(payload)
        size = -(-len(payload) // packets)
        lost = self.drop.pop(0) if packets > 1 and self.drop else set()
        for i in range(packets):
            if i in lost:
                continue
            chunk = payload[i * size:(i + 1) * size]
            self.dev.on_midi(mido.Message("sysex", data=P.build(chunk, total=packets, index=i)))

    def dump(self):
        """Same layout as a real dump (see tests/fixtures/dump_real_p34.json)."""
        st = self.edit

        def rec(tag, data):
            return struct.pack("<HH", tag, len(data)) + bytes(data)

        params = bytearray(P.PARAM_STRIDE * 10)
        for m, ps in st["params"].items():
            for a, v in ps.items():
                struct.pack_into("<f", params, P.PARAM_STRIDE * m + 4 * a, v)
        codes = b"".join(struct.pack("<I", st["codes"][m]) for m in range(10))
        hdr = rec(1, struct.pack("<I", 2)) + rec(2, b"\x0a\x45\x4d\x51")
        g0 = rec(0x1001, struct.pack("<I", 10)) + rec(0x1002, struct.pack("<I", 8)) + rec(0x1003, struct.pack("<I", 2))
        g1 = rec(P.T_VOLUME, struct.pack("<I", st["volume"])) + rec(P.T_TEMPO, struct.pack("<I", 120))
        g2 = (rec(P.T_STATES, struct.pack("<I", st["mask"])) + rec(P.T_CHAIN, bytes(st["chain"] + [9]))
              + rec(P.T_EFFECTS, codes) + rec(P.T_PARAMS, params))
        body = rec(0, g0) + rec(1, g1) + rec(2, g2) + rec(3, bytes(8))
        return bytes([P.GET, P.Q_CURRENT_DUMP]) + struct.pack("<HH", 0xFF, len(hdr)) + hdr + body

    def names(self):
        b = bytearray([P.GET, P.Q_PRESET_NAMES])
        for i, s in enumerate(self.slots):
            name = bytes(P.encode_name(s["name"])) if s["name"] else bytes(10)
            b += struct.pack("<I", i) + name + bytes(6)
        return bytes(b) + bytes(12)

    # ---- MIDI port interface
    def send(self, msg):
        f = P.parse_frame(list(msg.data))
        assert f.crc_ok
        self.sent.append(f)
        p = f.payload
        if p[0] == P.GET:
            if p[1] == P.Q_CURRENT_INDEX:
                self.reply([P.GET, P.CMD_SELECT, self.current, 0, 0, 0])
            elif p[1] == P.Q_CURRENT_DUMP:
                self.reply(self.dump(), packets=26)
            elif p[1] == P.Q_PRESET_NAMES:
                self.reply(self.names(), packets=106)
            return
        u = lambda o: struct.unpack_from("<I", p, o)[0]  # noqa: E731
        cmd = p[1]
        if cmd == P.CMD_SELECT:
            self.current = u(2)
            self.edit = copy.deepcopy(self.slots[self.current])
        elif cmd == P.CMD_STATE:
            m, on = u(2), u(6)
            self.edit["mask"] = (self.edit["mask"] | (1 << m)) if on else (self.edit["mask"] & ~(1 << m))
        elif cmd == P.CMD_EFFECT:
            m, code = u(2), u(10)
            self.edit["codes"][m] = code
            model = self.dev.model_by_code(m, code)
            self.edit["params"][m] = {q["alg"]: float(q["default"]) for q in model["params"]}
        elif cmd == P.CMD_PARAM:
            self.edit["params"].setdefault(u(2), {})[u(6)] = struct.unpack_from("<f", p, 10)[0]
        elif cmd == P.CMD_CHAIN:
            self.edit["chain"] = list(p[2:11])
        elif cmd == P.CMD_PRESET_VOLUME:
            self.edit["volume"] = p[6]
        elif cmd == P.CMD_SAVE:
            idx = u(2)
            self.slots[idx] = copy.deepcopy(self.edit)
            self.slots[idx]["name"] = P.decode_name(p[6:16])
            self.current = idx
            self.reply([P.ACK, P.CMD_ACK, 0])

    def close(self):
        pass
