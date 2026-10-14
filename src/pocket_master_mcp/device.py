"""MIDI connection to a Sonicake Pocket Master and the operations built on top of it."""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import protocol as P

log = logging.getLogger("pocket_master_mcp")

PORT_HINTS = ["pocket", "sonicake", "sonic master", "qme"]
SEND_GAP_S = 0.015
REPLY_TIMEOUT_S = 1.5
DUMP_TIMEOUT_S = 1.5       # per attempt; the dump (26 packets) arrives in ~0.03 s
NAMES_TIMEOUT_S = 2.0      # per attempt; the names (106 packets) arrive in ~0.1 s
QUERY_RETRIES = 2          # USB MIDI occasionally drops packets of a multi-packet reply; queries are re-sent
PARTIAL_TTL_S = 1.0        # a multi-packet reply arrives within ~0.1 s; older partial sets are stale

BLOCK_ALIASES = {
    "gate": "nr", "noisegate": "nr", "ng": "nr", "noise": "nr",
    "fx": "fx1", "effect1": "fx1", "pre": "fx1", "comp": "fx1", "wah": "fx1",
    "drive": "drv", "od": "drv", "dist": "drv", "distortion": "drv", "overdrive": "drv", "fuzz": "drv",
    "amplifier": "amp", "clone": "amp",
    "cab": "ir", "cabinet": "ir", "impulse": "ir",
    "equalizer": "eq",
    "effect2": "fx2", "mod": "fx2", "modulation": "fx2", "post": "fx2",
    "delay": "dly", "echo": "dly",
    "reverb": "rvb", "rev": "rvb", "verb": "rvb",
}


def _norm(s: Any) -> str:
    return re.sub(r"[\s\-_.()/]", "", str(s).lower())


def load_device_map(path: str | os.PathLike | None = None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(resources.files(__package__).joinpath("data/device_map.json").read_text(encoding="utf-8"))


def data_dir() -> Path:
    d = Path(os.environ.get("PM_DATA_DIR") or Path.home() / ".pocket-master-mcp").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


class Reassembler:
    """Collects the packets of multi-packet responses (same `total`, indexes 0..total-1).
    Partial sets older than PARTIAL_TTL_S are dropped, so packets of two replies are never mixed."""

    def __init__(self) -> None:
        self.pending: dict[int, dict[int, P.Frame]] = {}
        self.started: dict[int, float] = {}

    def feed(self, f: P.Frame, now: float | None = None) -> P.Frame | None:
        if f.total <= 1:
            return f
        now = time.time() if now is None else now
        if f.index == 0 or now - self.started.get(f.total, now) > PARTIAL_TTL_S:
            self.pending[f.total] = {}
            self.started[f.total] = now
        parts = self.pending.setdefault(f.total, {})
        self.started.setdefault(f.total, now)
        parts[f.index] = f
        if len(parts) == f.total:
            del self.pending[f.total]
            self.started.pop(f.total, None)
            payload = b"".join(parts[i].payload for i in range(f.total))
            return P.Frame(1, 0, payload, all(p.crc_ok for p in parts.values()))
        return None


class PocketMaster:
    """Stateful connection. `state` mirrors the edit buffer (from dumps and device notifications)."""

    def __init__(self, device_map: dict[str, Any] | None = None) -> None:
        self.map = device_map or load_device_map(os.environ.get("PM_DEVICE_MAP"))
        self.blocks = {b["key"]: b for b in self.map["blocks"]}
        self.lock = threading.Lock()
        self.port = None
        self.inport = None
        self.port_name: str | None = None
        self.rx: list[tuple[float, P.Frame]] = []      # complete (reassembled) frames
        self.raw: list[tuple[float, list[int]]] = []   # raw SysEx data, for debugging
        self._asm = Reassembler()
        self.current: int | None = None                # current preset index
        self.modified: bool | None = None
        self.state: dict[str, Any] = {}
        self.names: dict[int, str] = {}

    # ------------------------------------------------------------------ MIDI
    @staticmethod
    def list_ports() -> dict[str, list[str]]:
        import mido
        return {"outputs": list(mido.get_output_names()), "inputs": list(mido.get_input_names())}

    def connect(self, hint: str | None = None) -> str:
        import mido
        outs, ins = mido.get_output_names(), mido.get_input_names()
        hints = [hint] if hint else ([os.environ["PM_PORT"]] if os.environ.get("PM_PORT") else PORT_HINTS)
        match = next((n for h in hints for n in outs if h.lower() in n.lower()), None)
        candidates = [match] if match else [n for n in outs if n in ins]
        self.close()
        for name in candidates:
            self.port = mido.open_output(name)
            in_name = name if name in ins else next((n for h in hints for n in ins if h.lower() in n.lower()), None)
            self.inport = mido.open_input(in_name, callback=self.on_midi) if in_name else None
            self.port_name = name
            if match or self.probe():
                log.info("connected to %s (input %s)", name, in_name)
                try:
                    self.refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("initial sync failed: %s", e)
                return name
            self.close()
        raise RuntimeError(f"Pocket Master not found. MIDI outputs: {outs or 'none'}. Check the USB cable, "
                           "that the unit is on, and set PM_PORT to a part of the port name if it is unusual.")

    def probe(self) -> bool:
        """True if something on the open port answers a 'current preset' query with a valid frame."""
        t0 = time.time()
        self.sysex(P.query(P.Q_CURRENT_INDEX))
        return self.wait_frame(t0, lambda f: f.crc_ok, timeout=0.6) is not None

    def close(self) -> None:
        for p in (self.port, self.inport):
            try:
                if p is not None:
                    p.close()
            except Exception:  # noqa: BLE001
                pass
        self.port = self.inport = None

    def ensure(self) -> None:
        if self.port is None:
            self.connect()

    def on_midi(self, msg: Any) -> None:
        if msg.type != "sysex":
            return
        data = list(msg.data)
        now = time.time()
        self.raw.append((now, data))
        del self.raw[:-2000]
        try:
            f = P.parse_frame(data)
        except ValueError:
            return
        f = self._asm.feed(f, now)
        if f is None:
            return
        self.rx.append((now, f))
        del self.rx[:-2000]
        try:
            self._track(f)
        except Exception as e:  # noqa: BLE001
            log.debug("could not track %s: %s", f.payload.hex(), e)

    def sysex(self, data: list[int]) -> None:
        import mido
        with self.lock:
            if self.port is None:
                raise RuntimeError("not connected")
            self.port.send(mido.Message("sysex", data=list(data)))
            time.sleep(SEND_GAP_S)

    def wait_frame(self, since: float, pred: Callable[[P.Frame], bool],
                   timeout: float = REPLY_TIMEOUT_S) -> P.Frame | None:
        end = time.time() + timeout
        while time.time() < end:
            for t, f in list(self.rx):
                if t >= since and pred(f):
                    return f
            time.sleep(0.01)
        return None

    def request(self, kind: int, pred: Callable[[P.Frame], bool] | None = None,
                timeout: float = REPLY_TIMEOUT_S, retries: int | None = None) -> P.Frame | None:
        """Send a query and return the first matching reply (default: any non-ACK frame).
        The query is re-sent up to `retries` times if no complete reply arrives within `timeout`."""
        self.ensure()
        pred = pred or (lambda f: f.payload[:1] != bytes([P.ACK]))
        retries = QUERY_RETRIES if retries is None else retries
        for attempt in range(retries + 1):
            t0 = time.time()
            self.sysex(P.query(kind))
            f = self.wait_frame(t0, pred, timeout)
            if f is not None:
                return f
            if attempt < retries:
                log.info("no complete reply to query %#04x, retrying (%d/%d)", kind, attempt + 1, retries)
        return None

    def send(self, data: list[int], wait_ack: bool = False, timeout: float = 3.0) -> bool | None:
        self.ensure()
        t0 = time.time()
        self.sysex(data)
        if not wait_ack:
            return None
        f = self.wait_frame(t0, lambda f: f.kind == (P.ACK, P.CMD_ACK), timeout)
        return None if f is None else (len(f.payload) > 2 and f.payload[2] == 0)

    # ----------------------------------------------------- model / parameter maps
    def block(self, name: str) -> dict[str, Any]:
        key = BLOCK_ALIASES.get(_norm(name), _norm(name))
        if key not in self.blocks:
            raise ValueError(f"unknown block '{name}'. Blocks: {', '.join(self.blocks)}")
        return self.blocks[key]

    def models(self, key: str) -> list[dict[str, Any]]:
        return self.map["models"].get(key, [])

    def find_model(self, key: str, model: int | str) -> tuple[str, dict[str, Any]]:
        """Resolve a model name/id for a block. For 'amp', clone profiles are accepted too.
        Returns (models_key, model) where models_key is 'clone' for clone profiles."""
        pools = [key] + (["clone"] if key == "amp" else [])
        want = _norm(model)
        for pool in pools:
            for m in self.models(pool):
                if _norm(m["name"]) == want or str(m["id"]) == str(model).strip():
                    return pool, m
        for pool in pools:  # unique prefix / substring match
            hits = [m for m in self.models(pool) if want and want in _norm(m["name"])]
            if len(hits) == 1:
                return pool, hits[0]
        names = [m["name"] for p in pools for m in self.models(p)]
        raise ValueError(f"unknown model '{model}' for {key}. Models: {', '.join(names)}")

    def model_by_code(self, module: int, code: int) -> dict[str, Any] | None:
        if module == P.CLONE_MODULE:
            return next((m for m in self.models("clone") if m["code"] & 0xFFFFFF == code & 0xFFFFFF), None)
        key = next((b["key"] for b in self.map["blocks"] if b["module"] == module), None)
        if key and len(self.models(key)) == 1:
            return self.models(key)[0]
        return next((m for m in self.models(key) if m["code"] == code), None) if key else None

    @staticmethod
    def param_spec(model: dict[str, Any], knob: int | str) -> dict[str, Any]:
        want = _norm(knob)
        for p in model["params"]:
            if _norm(p["name"]) == want:
                return p
        if str(knob).strip().isdigit():
            i = int(knob)
            if 1 <= i <= len(model["params"]):
                return model["params"][i - 1]
        raise ValueError(f"{model['name']} has no parameter '{knob}'. "
                         f"Parameters: {', '.join(p['name'] for p in model['params']) or 'none'}")

    @staticmethod
    def encode_value(p: dict[str, Any], value: Any) -> float:
        t = p.get("type")
        if t == "switch":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("on", "true", "yes", "1"):
                    return 1.0
                if v in ("off", "false", "no", "0"):
                    return 0.0
                raise ValueError(f"{p['name']} is a switch: use on/off")
            return 1.0 if value else 0.0
        if t == "select":
            opts = p["options"]
            if isinstance(value, str) and not value.strip().isdigit():
                for i, o in enumerate(opts):
                    if _norm(o) == _norm(value):
                        return float(i)
                raise ValueError(f"{p['name']} options: {', '.join(opts)}")
            i = int(value)
            if not 0 <= i < len(opts):
                raise ValueError(f"{p['name']} options: {', '.join(opts)} (0..{len(opts) - 1})")
            return float(i)
        v = float(value)
        lo, hi, step = p.get("min", 0), p.get("max", 100), p.get("step", 1)
        if not lo <= v <= hi:
            raise ValueError(f"{p['name']} must be {lo}..{hi}{' ' + p['unit'] if p.get('unit') else ''}, got {value}")
        if step:
            v = lo + round((v - lo) / step) * step
            v = round(v, 4)
        return float(v)

    @staticmethod
    def display_value(p: dict[str, Any], v: float | None) -> Any:
        if v is None:
            return None
        if p.get("type") == "switch":
            return "on" if v >= 0.5 else "off"
        if p.get("type") == "select":
            i = int(round(v))
            return p["options"][i] if 0 <= i < len(p["options"]) else i
        step = p.get("step", 1)
        return int(round(v)) if float(step).is_integer() else round(v, 2)

    # ------------------------------------------------------------- state mirror
    def _module_key(self, module: int) -> str:
        return next((b["key"] for b in self.map["blocks"] if b["module"] == module), f"m{module}")

    def _track(self, f: P.Frame) -> None:
        """Update the state mirror from notifications the device sends (knob turns, preset changes)."""
        p, cmd = f.payload, f.command
        if f.kind is None or p[0] not in (P.SET, P.GET):
            return
        if cmd == P.CMD_SELECT and len(p) >= 3:
            self.current = p[2]
        elif cmd == P.Q_MODIFIED and len(p) >= 3:
            self.modified = bool(p[2])
        if not self.state:
            return
        blocks = self.state.setdefault("blocks", {})
        if cmd == P.CMD_STATE and len(p) >= 10:
            mod, val = int.from_bytes(p[2:6], "little"), int.from_bytes(p[6:10], "little")
            if mod == P.AMP_MODE_MODULE:
                self.state["amp_mode"] = "clone" if val else "normal"
            elif mod < 9:
                blocks.setdefault(self._module_key(mod), {})["on"] = bool(val)
        elif cmd == P.CMD_EFFECT and len(p) >= 10:
            mod = int.from_bytes(p[2:6], "little")
            m = self.model_by_code(mod, int.from_bytes(p[-4:], "little"))
            if m and (mod < 9 or mod == P.CLONE_MODULE):
                key = "amp" if mod == P.CLONE_MODULE else self._module_key(mod)
                entry = blocks.setdefault(key, {})
                entry["model"] = m["name"]
                entry["params"] = {q["name"]: self.display_value(q, q.get("default")) for q in m["params"]}
        elif cmd == P.CMD_PARAM and len(p) >= 14:
            mod, alg = int.from_bytes(p[2:6], "little"), int.from_bytes(p[6:10], "little")
            key = "amp" if mod == P.CLONE_MODULE else self._module_key(mod)
            pool = "clone" if mod == P.CLONE_MODULE else key
            name = blocks.get(key, {}).get("model")
            m = next((x for x in self.models(pool) if x["name"] == name), None)
            q = next((x for x in m["params"] if x["alg"] == alg), None) if m else None
            if q:
                v = struct.unpack("<f", p[10:14])[0]
                blocks[key].setdefault("params", {})[q["name"]] = self.display_value(q, v)

    # ---------------------------------------------------------------- reading
    def read_current_index(self) -> int | None:
        f = self.request(P.Q_CURRENT_INDEX, lambda f: f.command == P.CMD_SELECT and len(f.payload) >= 3)
        if f is None:
            f = self.request(P.Q_CURRENT_INDEX)
        if f is not None and len(f.payload) >= 3 and f.payload[2] < P.PRESET_COUNT:
            self.current = f.payload[2]
        return self.current

    def read_dump(self) -> bytes:
        f = self.request(P.Q_CURRENT_DUMP, lambda f: len(f.payload) > 200, timeout=DUMP_TIMEOUT_S)
        if f is None:
            raise RuntimeError("the device did not send the preset dump")
        return f.payload

    def decode_dump(self, payload: bytes) -> dict[str, Any]:
        d = P.dump_fields(payload)
        out: dict[str, Any] = {}
        mask = d.get("mask")
        clone = bool(mask & (1 << P.CLONE_MODULE)) if mask is not None else None
        if d.get("chain"):
            out["chain"] = [P.MODULES[m] for m in d["chain"]]
        if clone is not None:
            out["amp_mode"] = "clone" if clone else "normal"
        if d.get("volume") is not None:
            out["preset_volume"] = d["volume"]
        if d.get("tempo") is not None:
            out["tempo_bpm"] = d["tempo"]
        codes, params = d.get("codes") or [], d.get("params") or b""
        blocks: dict[str, Any] = {}
        for b in self.map["blocks"]:
            mod = b["module"]
            src = P.CLONE_MODULE if (mod == 3 and clone) else mod
            code = codes[src] if src < len(codes) else None
            model = self.model_by_code(src, code) if code is not None else None
            entry: dict[str, Any] = {}
            if mask is not None:
                entry["on"] = bool(mask & (1 << mod))
            if model is None:
                entry["model"] = f"unknown (code {code:#010x})" if code is not None else "unknown"
            else:
                entry["model"] = model["name"]
                entry["params"] = {p["name"]: self.display_value(p, P.param_from_block(params, src, p["alg"]))
                                   for p in model["params"]}
            blocks[b["key"]] = entry
        out["blocks"] = blocks
        return out

    def refresh(self) -> dict[str, Any]:
        """Read preset number + edit buffer from the device into `state`."""
        self.ensure()
        idx = self.read_current_index()
        st = self.decode_dump(self.read_dump())
        if idx is not None:
            st = {"preset": P.preset_label(idx), **({"name": self.names[idx]} if idx in self.names else {}), **st}
        self.state = st
        return st

    def read_names(self) -> dict[int, str]:
        f = self.request(P.Q_PRESET_NAMES, lambda f: len(f.payload) > 100, timeout=NAMES_TIMEOUT_S)
        if f is None:
            raise RuntimeError("the device did not send the preset names")
        names = P.preset_name_records(f.payload)
        if len(names) < P.PRESET_COUNT // 2 or sorted(names) != list(range(len(names))):
            # unexpected layout: fall back to scanning for name-like blocks
            names = {i: n for i, (_, n) in enumerate(P.scan_names(f.payload))}
        self.names = {i: n for i, n in names.items() if i < P.PRESET_COUNT}
        return self.names

    def read_slot_names(self, kind: int) -> list[str]:
        f = self.request(kind, lambda f: len(f.payload) > 20, timeout=2.0)
        return P.slot_names(f.payload) if f else []

    # ---------------------------------------------------------------- editing
    def _current_model(self, key: str) -> tuple[int, dict[str, Any]]:
        """(module id that carries the params, model) for a block, from the state mirror."""
        if not self.state:
            self.refresh()
        b = self.blocks[key]
        name = self.state.get("blocks", {}).get(key, {}).get("model")
        clone = key == "amp" and self.state.get("amp_mode") == "clone"
        pool = "clone" if clone else key
        model = next((m for m in self.models(pool) if m["name"] == name), None)
        if model is None:
            self.refresh()
            name = self.state.get("blocks", {}).get(key, {}).get("model")
            model = next((m for m in self.models(pool) if m["name"] == name), None)
        if model is None:
            raise RuntimeError(f"current model of {key} is unknown; select one with set_model first")
        return (P.CLONE_MODULE if clone else b["module"]), model

    def set_model(self, block: str, model: int | str, on: bool | None = True) -> str:
        b = self.block(block)
        key = b["key"]
        if key == "nr":
            raise ValueError("NR has a single model (Gate); use set_block / set_param")
        pool, m = self.find_model(key, model)
        if key == "amp":
            clone = pool == "clone"
            self.send(P.amp_mode(clone))
            self.send(P.select_effect(P.CLONE_MODULE if clone else 3, m["code"]))
            self.state["amp_mode"] = "clone" if clone else "normal"
        else:
            self.send(P.select_effect(b["module"], m["code"]))
        if on is not None:
            self.send(P.module_state(b["module"], on))
        if self.state:
            entry = self.state.setdefault("blocks", {}).setdefault(key, {})
            entry.update({"model": m["name"],
                          "params": {p["name"]: self.display_value(p, p.get("default")) for p in m["params"]}})
            if on is not None:
                entry["on"] = bool(on)
        state = "" if on is None else (" ON" if on else " OFF")
        return f"{key}: {m['name']}{state}"

    def set_block(self, block: str, on: bool) -> str:
        b = self.block(block)
        self.send(P.module_state(b["module"], on))
        if self.state:
            self.state.setdefault("blocks", {}).setdefault(b["key"], {})["on"] = bool(on)
        return f"{b['key']}: {'ON' if on else 'OFF'}"

    def set_param(self, block: str, param: int | str, value: Any) -> str:
        b = self.block(block)
        module, model = self._current_model(b["key"])
        p = self.param_spec(model, param)
        v = self.encode_value(p, value)
        self.send(P.set_param(module, p["alg"], v))
        shown = self.display_value(p, v)
        if self.state:
            self.state.setdefault("blocks", {}).setdefault(b["key"], {}).setdefault("params", {})[p["name"]] = shown
        return f"{b['key']} ({model['name']}).{p['name']} = {shown}{' ' + p['unit'] if p.get('unit') else ''}"

    def set_chain(self, order: list[str]) -> str:
        ids = []
        for name in order:
            ids.append(self.block(name)["module"])
        if len(ids) == 5 and all(i not in P.FIXED_BLOCK for i in ids):
            raise ValueError("give the full chain incl. the DRV-AMP-IR-EQ block, e.g. "
                             "['nr','fx1','drv','amp','ir','eq','fx2','dly','rvb']")
        self.send(P.set_chain(ids))
        if self.state:
            self.state["chain"] = [P.MODULES[i] for i in ids]
        return "chain: " + " > ".join(P.MODULES[i] for i in ids)

    def set_preset_volume(self, value: int) -> str:
        self.send(P.preset_volume(int(value)))
        if self.state:
            self.state["preset_volume"] = int(value)
        return f"preset volume = {int(value)}"

    def select(self, index: int) -> dict[str, Any]:
        self.send(P.select_preset(index))
        self.current = index
        time.sleep(0.25)
        return self.refresh()

    def save(self, index: int, name: str) -> dict[str, Any]:
        ok = self.send(P.save_preset(index, name), wait_ack=True)
        result: dict[str, Any] = {"preset": P.preset_label(index), "name": name,
                                  "ack": {True: "ok", False: "device reported failure", None: "no ack"}[ok]}
        if ok:
            self.names[index] = name
            self.current = index
            self.modified = False
        try:
            names = self.read_names()
            result["verified"] = names.get(index) == name
        except Exception as e:  # noqa: BLE001
            result["verified"] = f"could not read names back: {e}"
        return result

    def apply_spec(self, spec: dict[str, Any]) -> list[str]:
        """Apply a preset spec (see server.apply_preset) to the edit buffer."""
        out = []
        if spec.get("chain"):
            out.append(self.set_chain(spec["chain"]))
        for block, bs in (spec.get("blocks") or {}).items():
            if isinstance(bs, bool):
                bs = {"on": bs}
            if bs.get("model"):
                out.append(self.set_model(block, bs["model"], bs.get("on", True)))
            elif "on" in bs:
                out.append(self.set_block(block, bool(bs["on"])))
            for k, v in (bs.get("params") or bs.get("knobs") or {}).items():
                out.append(self.set_param(block, k, v))
        if spec.get("preset_volume") is not None:
            out.append(self.set_preset_volume(spec["preset_volume"]))
        return out
