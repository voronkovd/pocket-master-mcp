"""Device logic against a fake Pocket Master."""
import pytest

from pocket_master_mcp import device as D
from pocket_master_mcp import protocol as P

from fake_device import FakePocketMaster


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(D, "SEND_GAP_S", 0)
    monkeypatch.setattr(D, "DUMP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(D, "NAMES_TIMEOUT_S", 0.05)
    d = D.PocketMaster()
    d.port = FakePocketMaster(d, d.map)
    d.refresh()
    return d


def test_refresh_decodes_dump(dev):
    st = dev.state
    assert st["preset"] == "P04"
    assert st["chain"] == P.MODULES
    assert st["amp_mode"] == "normal" and st["preset_volume"] == 75
    assert st["blocks"]["amp"]["model"] == "TWD Deluxe"
    assert st["blocks"]["drv"]["model"] == "Scream" and st["blocks"]["drv"]["on"] is True
    assert st["blocks"]["fx2"]["on"] is False
    assert st["blocks"]["dly"]["params"]["Time"] == 500


def test_set_model_param_and_aliases(dev):
    assert dev.set_model("drive", "jp dist") == "drv: JP Dist ON"
    dev.set_param("drv", "gain", 63)
    dev.set_model("delay", "Analog")
    dev.set_param("delay", "time", 380.4)   # rounded to the 1 ms step
    st = dev.refresh()
    assert st["blocks"]["drv"]["model"] == "JP Dist" and st["blocks"]["drv"]["params"]["Gain"] == 63
    assert st["blocks"]["dly"]["params"]["Time"] == 380
    with pytest.raises(ValueError):
        dev.set_param("dly", "time", 5000)
    with pytest.raises(ValueError):
        dev.set_model("amp", "Nonexistent Amp")


def test_select_and_switch_params(dev):
    dev.set_model("fx1", "Touch Wah")
    dev.set_param("fx1", "Mode", "Bass")
    dev.set_model("fx2", "Boost", on=True)
    dev.set_param("fx2", "+3dB", "off")
    st = dev.refresh()
    assert st["blocks"]["fx1"]["params"]["Mode"] == "Bass"
    assert st["blocks"]["fx2"]["params"]["+3dB"] == "off" and st["blocks"]["fx2"]["on"] is True


def test_clone_profile_goes_to_module_9(dev):
    dev.set_model("amp", "User Profile 2")
    frames = [f for f in dev.port.sent if f.payload[0] == P.SET]
    assert frames[-3].payload == bytes([P.SET, P.CMD_STATE] + P.u32(9) + P.u32(1))   # amp mode clone
    assert frames[-2].payload[:6] == bytes([P.SET, P.CMD_EFFECT] + P.u32(9))
    st = dev.refresh()
    assert st["amp_mode"] == "clone" and st["blocks"]["amp"]["model"] == "User Profile 2"
    dev.set_model("amp", "Brit 800")
    st = dev.refresh()
    assert st["amp_mode"] == "normal" and st["blocks"]["amp"]["model"] == "Brit 800"


def test_chain(dev):
    dev.set_chain(["fx1", "nr", "drv", "amp", "ir", "eq", "rvb", "dly", "fx2"])
    assert dev.refresh()["chain"] == ["FX1", "NR", "DRV", "AMP", "IR", "EQ", "RVB", "DLY", "FX2"]
    with pytest.raises(ValueError):
        dev.set_chain(["nr", "drv", "fx1", "amp", "ir", "eq", "fx2", "dly", "rvb"])


def test_names_keep_slot_positions(dev):
    names = dev.read_names()
    assert names[1] == "Slot 2" and names[0] == "" and names[9] == "" and names[10] == "Slot 11" and len(names) == 100


def test_apply_and_save(dev):
    log = dev.apply_spec({"blocks": {"amp": {"model": "Brit 800", "params": {"Gain": 72}},
                                     "rvb": {"on": False}}, "preset_volume": 66})
    assert len(log) == 4
    res = dev.save(11, "Brit Lead")
    assert res == {"preset": "P12", "name": "Brit Lead", "ack": "ok", "verified": True}
    dev.select(0)
    st = dev.select(11)
    assert st["blocks"]["amp"]["params"]["Gain"] == 72 and st["preset_volume"] == 66
    assert st["blocks"]["rvb"]["on"] is False


def test_notifications_update_state(dev):
    import mido
    dev.on_midi(mido.Message("sysex", data=P.build([P.GET, P.CMD_PARAM] + P.u32(3) + P.u32(0) + P.f32(81))))
    assert dev.state["blocks"]["amp"]["params"]["Gain"] == 81
    dev.on_midi(mido.Message("sysex", data=P.build([P.GET, P.CMD_SELECT] + P.u32(17))))
    assert dev.current == 17


def test_example_presets_apply(dev):
    import json
    from pathlib import Path
    for f in sorted((Path(__file__).parents[1] / "examples" / "presets").glob("*.json")):
        spec = json.loads(f.read_text())
        dev.apply_spec(spec)
        st = dev.refresh()
        for key, b in spec["blocks"].items():
            if "model" in b:
                assert st["blocks"][key]["model"] == b["model"], (f.name, key)
            assert st["blocks"][key]["on"] == b.get("on", True), (f.name, key)
            for k, v in (b.get("params") or {}).items():
                assert st["blocks"][key]["params"][k] == v, (f.name, key, k)


def test_decode_real_dump():
    import json
    from pathlib import Path
    fx = json.loads((Path(__file__).parent / "fixtures" / "dump_real_p34.json").read_text())
    st = D.PocketMaster().decode_dump(bytes.fromhex(fx["payload"]))
    assert st["chain"] == ["NR", "FX2", "FX1", "DRV", "AMP", "IR", "EQ", "DLY", "RVB"]
    assert st["amp_mode"] == "normal" and st["preset_volume"] == 35 and st["tempo_bpm"] == 120
    b = st["blocks"]
    assert b["nr"] == {"on": True, "model": "Gate", "params": {"THRE": 10}}
    assert b["fx1"] == {"on": True, "model": "Boost", "params": {"Gain": 28, "+3dB": "on", "Bright": "on"}}
    assert b["drv"]["model"] == "Scream" and b["drv"]["on"] is False
    assert b["drv"]["params"] == {"Gain": 40, "Tone": 70, "Vol": 50}
    assert b["amp"]["model"] == "Voks 30N" and b["amp"]["params"]["Gain"] == 31
    assert b["dly"] == {"on": False, "model": "Pure", "params": {"Mix": 20, "Time": 500, "F.Back": 30}}
    assert b["rvb"]["on"] is True and b["rvb"]["model"] == "Hall"
    assert [k for k, v in b.items() if v["on"]] == ["nr", "fx1", "amp", "ir", "fx2", "rvb"]


def test_dump_retried_when_packets_are_lost(dev):
    dev.port.drop = [{0, 1}]                     # seen on a real unit: first two packets of a dump lost
    st = dev.refresh()
    assert st["blocks"]["amp"]["model"] == "TWD Deluxe"
    dumps = [f for f in dev.port.sent if f.payload[:2] == bytes([P.GET, P.Q_CURRENT_DUMP])]
    assert len(dumps) == 3                       # fixture refresh + lost reply + retry


def test_names_retried_when_packets_are_lost(dev):
    dev.port.drop = [{50}]
    assert dev.read_names()[1] == "Slot 2"


def test_dump_fails_after_retries(dev):
    dev.port.drop = [{0}] * (D.QUERY_RETRIES + 1)
    with pytest.raises(RuntimeError, match="preset dump"):
        dev.read_dump()


def test_stale_partial_reply_is_not_mixed_in():
    asm = D.Reassembler()
    old = [P.Frame(3, i, bytes([i]), True) for i in range(3)]
    new = [P.Frame(3, i, bytes([10 + i]), True) for i in range(3)]
    assert asm.feed(old[0], now=0.0) is None     # old reply loses packet 2
    assert asm.feed(old[1], now=0.0) is None
    assert asm.feed(new[1], now=5.0) is None     # new reply loses packet 0
    assert asm.feed(new[2], now=5.0) is None     # without the TTL this would complete as old0+new1+new2
    for f in new:
        out = asm.feed(f, now=6.0)
    assert out.payload == bytes([10, 11, 12])
