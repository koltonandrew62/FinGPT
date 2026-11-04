"""Home Assistant tools against a mocked REST API (httpx.MockTransport): listing, control, validation and the
approval gate. No real Home Assistant or network is involved."""
import json

import httpx
import pytest

from app.tools import TOOLS, ToolError, call_tool, smart_home as sh
from conftest import make_ctx, run

URL = "http://homeassistant.local:8123"
TOKEN = "llat-abc123"

STATES = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light", "brightness": 128}},
    {"entity_id": "light.living_room_lamp", "state": "off", "attributes": {"friendly_name": "Living Room Lamp"}},
    {"entity_id": "switch.coffee_maker", "state": "off", "attributes": {"friendly_name": "Coffee Maker"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door", "battery_level": 81}},
    {"entity_id": "cover.garage_door", "state": "closed", "attributes": {"friendly_name": "Garage Door", "device_class": "garage"}},
    {
        "entity_id": "climate.hallway",
        "state": "heat",
        "attributes": {"friendly_name": "Hallway Thermostat", "temperature": 70, "current_temperature": 68.5,
                       "hvac_modes": ["off", "heat", "cool"], "hvac_action": "heating"},
    },
    {"entity_id": "media_player.tv", "state": "playing", "attributes": {"friendly_name": "TV", "media_title": "Dune", "volume_level": 0.4}},
    {"entity_id": "sensor.outdoor_temp", "state": "51", "attributes": {"friendly_name": "Outdoor Temp", "unit_of_measurement": "°F"}},
    {"entity_id": "sensor.phone_battery", "state": "77", "attributes": {"friendly_name": "Phone Battery"}},
    {"entity_id": "update.core", "state": "off", "attributes": {"friendly_name": "Core Update"}},
]


@pytest.fixture(autouse=True)
def fresh_gate_log():
    sh._GATE_SEEN.clear()
    yield
    sh._GATE_SEEN.clear()


@pytest.fixture(autouse=True)
def ha_env(monkeypatch):
    for key in ("HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_CONFIRM_DOMAINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME_ASSISTANT_URL", URL)
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", TOKEN)


class FakeHA:
    def __init__(self, states=STATES, service_status=200, service_body=None):
        self.states, self.service_status, self.service_body = states, service_status, service_body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "homeassistant.local" and request.url.port == 8123
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, text="401: Unauthorized")
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=self.states)
        if request.method == "GET" and request.url.path.startswith("/api/states/"):
            wanted = request.url.path.rsplit("/", 1)[1]
            for st in self.states:
                if st["entity_id"] == wanted:
                    return httpx.Response(200, json=st)
            return httpx.Response(404, json={"message": "Entity not found."})
        if request.method == "POST" and request.url.path.startswith("/api/services/"):
            if self.service_body is not None:
                return httpx.Response(self.service_status, json=self.service_body)
            body = json.loads(request.content)
            ids = body["entity_id"] if isinstance(body["entity_id"], list) else [body["entity_id"]]
            return httpx.Response(200, json=[{"entity_id": e, "state": "on", "attributes": {"friendly_name": e}} for e in ids])
        return httpx.Response(404, json={"message": "Not found"})


def ctx_for(handler):
    return make_ctx(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def tool(name, ctx, **args):
    return json.loads(run(call_tool(name, args, ctx)))


def gate(**args):
    return run(sh.control_gate(make_ctx(), args))


# ------------------------------------------------------------------ configuration
def test_not_configured_explains_token(monkeypatch):
    monkeypatch.delenv("HOME_ASSISTANT_TOKEN")
    out = tool("smart_home_devices", ctx_for(FakeHA()))
    assert "HOME_ASSISTANT_URL" in out["error"] and "Long-lived access tokens" in out["error"]
    assert "Security" in out["error"]


def test_url_normalisation(monkeypatch, isolated):
    monkeypatch.delenv("HOME_ASSISTANT_URL")
    monkeypatch.delenv("HOME_ASSISTANT_TOKEN")
    isolated["env_path"].write_text(f"HOME_ASSISTANT_URL=homeassistant.local:8123/api/\nHOME_ASSISTANT_TOKEN=Bearer {TOKEN}\n")
    assert sh._ha_config() == (URL, TOKEN)
    monkeypatch.setenv("HOME_ASSISTANT_URL", "https://ha.example.net/lovelace/0")
    assert sh._ha_config()[0] == "https://ha.example.net"
    monkeypatch.setenv("HOME_ASSISTANT_URL", "ftp://ha")
    with pytest.raises(ToolError, match="isn't a valid address"):
        sh._ha_config()


def test_auth_and_network_errors(monkeypatch):
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", "wrong")
    assert "rejected the access token" in tool("smart_home_devices", ctx_for(FakeHA()))["error"]

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def not_ha(request):
        return httpx.Response(200, text="<html>router login</html>")

    assert "Couldn't reach Home Assistant at http://homeassistant.local:8123" in tool("smart_home_devices", ctx_for(down))["error"]
    assert "didn't respond in time" in tool("smart_home_devices", ctx_for(slow))["error"]
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", TOKEN)
    assert "not like the Home Assistant API" in tool("smart_home_devices", ctx_for(not_ha))["error"]


# ------------------------------------------------------------------ devices
def test_devices_unfiltered_lists_controllable():
    ha = FakeHA()
    out = tool("smart_home_devices", ctx_for(ha))
    ids = [d["entity_id"] for d in out["devices"]]
    assert ids[0].startswith("light.") and "sensor.outdoor_temp" not in ids and "update.core" not in ids
    assert out["other_domains"] == {"sensor": 2, "update": 1}
    assert out["count"] == 7 and out["status"] == "7 devices"
    (req,) = ha.requests
    assert req.method == "GET" and str(req.url) == f"{URL}/api/states"
    kitchen = next(d for d in out["devices"] if d["entity_id"] == "light.kitchen")
    assert kitchen == {"entity_id": "light.kitchen", "name": "Kitchen Light", "state": "on", "brightness": 128, "brightness_pct": 50}
    thermo = next(d for d in out["devices"] if d["entity_id"] == "climate.hallway")
    assert thermo["temperature"] == 70 and thermo["current_temperature"] == 68.5 and thermo["hvac_action"] == "heating"
    assert next(d for d in out["devices"] if d["entity_id"] == "media_player.tv")["media_title"] == "Dune"
    assert next(d for d in out["devices"] if d["entity_id"] == "lock.front_door")["battery_level"] == 81


def test_devices_query_and_domain():
    ctx = ctx_for(FakeHA())
    out = tool("smart_home_devices", ctx, query="the kitchen lights")
    assert [d["entity_id"] for d in out["devices"]] == ["light.kitchen"]
    out = tool("smart_home_devices", ctx, query="front door lock")
    assert [d["entity_id"] for d in out["devices"]] == ["lock.front_door"]
    out = tool("smart_home_devices", ctx, domain="lights")
    assert {d["entity_id"] for d in out["devices"]} == {"light.kitchen", "light.living_room_lamp"}
    out = tool("smart_home_devices", ctx, domain="sensor", query="outdoor")
    assert out["devices"][0]["unit"] == "°F" and out["count"] == 1
    out = tool("smart_home_devices", ctx, query="living room fan")
    assert out["devices"][0]["entity_id"] == "light.living_room_lamp" and "closest" in out["note"]
    out = tool("smart_home_devices", ctx, domain="vacuum")
    assert out["count"] == 0 and "no 'vacuum' devices" in out["note"]
    out = tool("smart_home_devices", ctx, domain="light", limit=1)
    assert len(out["devices"]) == 1 and out["truncated"] is True


# ------------------------------------------------------------------ control
def test_control_posts_service_with_entity():
    ha = FakeHA()
    out = tool("smart_home_control", ctx_for(ha), entity_id="light.kitchen", service="turn_on", data={"brightness_pct": 40})
    (req,) = ha.requests
    assert req.method == "POST" and str(req.url) == f"{URL}/api/services/light/turn_on"
    assert json.loads(req.content) == {"brightness_pct": 40, "entity_id": "light.kitchen"}
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert out["status"] == "Called light.turn_on on light.kitchen" and out["changed"][0]["entity_id"] == "light.kitchen"


def test_control_variants():
    ha = FakeHA()
    ctx = ctx_for(ha)
    tool("smart_home_control", ctx, entity_id="scene.movie_night", service="scene.turn_on")
    assert ha.requests[-1].url.path == "/api/services/scene/turn_on"
    tool("smart_home_control", ctx, entity_id="light.kitchen, light.living_room_lamp", service="Turn Off")
    assert ha.requests[-1].url.path == "/api/services/light/turn_off"
    assert json.loads(ha.requests[-1].content) == {"entity_id": ["light.kitchen", "light.living_room_lamp"]}
    # data as a JSON string; a stray entity_id inside data can't redirect the call
    tool("smart_home_control", ctx, entity_id="climate.hallway", service="set_temperature",
         data='{"temperature": 72, "entity_id": "lock.front_door"}')
    assert json.loads(ha.requests[-1].content) == {"temperature": 72, "entity_id": "climate.hallway"}
    tool("smart_home_control", ctx, entity_id="all", service="light.turn_off")
    assert json.loads(ha.requests[-1].content) == {"entity_id": "all"}
    tool("smart_home_control", ctx, entity_id="light.kitchen", service="homeassistant.toggle")
    assert ha.requests[-1].url.path == "/api/services/homeassistant/toggle"


def test_control_reports_ha_errors():
    ha = FakeHA(service_status=400, service_body={"message": "Service light.explode not found."})
    out = tool("smart_home_control", ctx_for(ha), entity_id="light.kitchen", service="explode")
    assert "refused light.explode (400: Service light.explode not found.)" in out["error"]
    ok_empty = FakeHA(service_body=[])
    out = tool("smart_home_control", ctx_for(ok_empty), entity_id="light.kitchen", service="turn_on")
    assert out["changed"] == [] and "moment" in out["note"]


@pytest.mark.parametrize(
    "entity_id, service, data, fragment",
    [
        ("light.kitchen", "../../config/core", None, "isn't a valid service"),
        ("light.kitchen", "light/../x.turn_on", None, "isn't a valid service"),
        ("light.kitchen", "turn_on?x=1", None, "isn't a valid service"),
        ("light.kitchen", "light.turn.on", None, "isn't a valid service"),
        ("light.kitchen", "", None, "isn't a valid service"),
        ("../states", "turn_on", None, "isn't a valid entity_id"),
        ("light", "turn_on", None, "isn't a valid entity_id"),
        ("light.kitchen;rm", "turn_on", None, "isn't a valid entity_id"),
        ("", "turn_on", None, "Say which device"),
        ("all", "turn_off", None, "give the service with its domain"),
        ("light.kitchen, switch.coffee_maker", "turn_on", None, "different kinds"),
        ("light.kitchen", "turn_on", "not json", "data must be a JSON object"),
        ("light.kitchen", "turn_on", [1, 2], "data must be a JSON object"),
        ("light.kitchen", "turn_on", {"area_id": "garage"}, "remove area_id"),
    ],
)
def test_control_validation(entity_id, service, data, fragment):
    ha = FakeHA()
    args = {"entity_id": entity_id, "service": service}
    if data is not None:
        args["data"] = data
    out = tool("smart_home_control", ctx_for(ha), **args)
    assert fragment in out["error"]
    assert ha.requests == []  # nothing reached Home Assistant


def test_parse_call_normalises():
    assert sh.parse_call("Light.Kitchen", " TURN_ON ") == ("light", "turn_on", ["light.kitchen"], {"entity_id": "light.kitchen"})
    assert sh.parse_call(["light.a", "light.b"], "toggle")[2] == ["light.a", "light.b"]


# ------------------------------------------------------------------ approval gate
@pytest.mark.parametrize(
    "entity_id, service",
    [
        ("light.kitchen", "turn_on"),
        ("light.kitchen", "light.turn_off"),
        ("switch.coffee_maker", "toggle"),
        ("climate.hallway", "set_temperature"),
        ("media_player.tv", "media_pause"),
        ("scene.movie_night", "scene.turn_on"),
        ("fan.bedroom", "set_percentage"),
        ("cover.garage_door", "stop_cover"),
        ("all", "light.turn_off"),
        ("light.garage", "turn_on"),
    ],
)
def test_gate_lets_everyday_actions_run(entity_id, service):
    assert gate(entity_id=entity_id, service=service) is None


@pytest.mark.parametrize(
    "entity_id, service, reason",
    [
        ("lock.front_door", "unlock", "unlocks something"),
        ("lock.front_door", "lock", "controls locks"),
        ("lock.front_door", "lock.open", "opens something"),
        ("alarm_control_panel.home", "alarm_disarm", "disarms an alarm"),
        ("alarm_control_panel.home", "alarm_arm_away", "alarm systems"),
        ("cover.garage_door", "open_cover", "opens something"),
        ("cover.garage_door", "close_cover", "garage doors"),
        ("cover.garage_door", "set_cover_position", "garage doors"),
        ("cover.garage_door", "homeassistant.turn_on", "garage doors"),
        ("lock.front_door", "homeassistant.toggle", "controls locks"),
        ("valve.water_main", "open_valve", "opens something"),
        ("siren.hallway", "turn_on", "sirens"),
        ("camera.porch", "turn_off", "cameras"),
        ("all", "homeassistant.turn_off", "every device"),
        ("light.kitchen", "homeassistant.restart", "Home Assistant itself"),
        ("switch.garage_door_relay", "turn_on", "door, gate"),
        ("button.gate_opener", "press", "door, gate"),
        ("script.unlock_back_door", "turn_on", "security-related"),
    ],
)
def test_gate_requires_approval_for_security(entity_id, service, reason):
    p = gate(entity_id=entity_id, service=service)
    assert p is not None, (entity_id, service)
    assert reason in p["body"]
    assert isinstance(p["title"], str) and p["title"]
    assert all(isinstance(f, list) and len(f) == 2 for f in p["fields"])
    assert p["fields"][0][1] == entity_id


def test_gate_preview_masks_codes():
    p = gate(entity_id="lock.front_door", service="lock.unlock", data={"code": "4321"})
    assert ["Action", "lock.unlock"] in p["fields"]
    assert ["Data", '{"code": "••••"}'] in p["fields"]
    assert "4321" not in json.dumps(p)


def test_gate_confirm_domains_setting(monkeypatch, isolated):
    assert gate(entity_id="switch.coffee_maker", service="turn_on") is None
    isolated["env_path"].write_text("HOME_ASSISTANT_CONFIRM_DOMAINS=switch, media_player.volume_set, light.nursery\n")
    p = gate(entity_id="switch.coffee_maker", service="turn_on")
    assert p and "switch is on your approval list" in p["body"]
    assert gate(entity_id="media_player.tv", service="volume_set", data={"volume_level": 1})
    assert gate(entity_id="media_player.tv", service="media_pause") is None
    assert gate(entity_id="light.nursery", service="turn_on")
    assert gate(entity_id="light.kitchen", service="turn_on") is None
    monkeypatch.setenv("HOME_ASSISTANT_CONFIRM_DOMAINS", "*")
    assert "every smart-home action" in gate(entity_id="light.kitchen", service="turn_on")["body"]


def test_gate_invalid_and_broken_inputs(monkeypatch):
    assert gate(entity_id="../x", service="turn_on") is None  # the tool rejects it before any request
    assert gate(service="turn_on") is None

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(sh, "_confirm_list", broken)
    p = gate(entity_id="light.kitchen", service="turn_on")
    assert p and "couldn't be completed" in p["body"]  # fail closed


def test_registered_with_gate():
    t = TOOLS["smart_home_control"]
    assert t.gate is sh.control_gate and t.preview is None
    assert TOOLS["smart_home_devices"].gate is None
    assert set(t.parameters["required"]) == {"entity_id", "service"}


# ------------------------------------------------------------------ review hardening: gate bypasses
@pytest.mark.parametrize(
    "entity_id, service, data, reason",
    [
        # "all" would also hit garage relays, gate buttons and security scripts
        ("all", "switch.turn_on", None, "every switch device"),
        ("all", "button.press", None, "every button"),
        ("all", "script.turn_on", None, "every script"),
        ("all", "scene.turn_on", None, "every scene"),
        # a script run as its own service, with an unrelated entity_id
        ("light.kitchen", "script.garage_door", None, "script.garage_door may operate a door"),
        # homeassistant.turn_on on an old-style group opens covers inside it
        ("group.garage_doors", "homeassistant.turn_on", None, "group"),
        # valves (water/gas) without the word "open" in the service
        ("valve.gas_main", "toggle", None, "valves"),
        ("valve.water_main", "set_valve_position", {"position": 100}, "valves"),
        # outside everyday device control
        ("notify.mobile_app_pixel", "notify.send_message", {"message": "hi"}, "sends a message"),
        ("update.core_update", "install", None, "software update"),
        ("automation.night_lock", "turn_off", None, "automation"),
        ("device_tracker.phone", "see", None, "tracked location"),
        ("light.kitchen", "shell_command.reboot", None, "runs a command"),
        ("tts.google", "tts.speak", {"message": "hello"}, "speaks out loud"),
        ("light.kitchen", "recorder.purge", None, "history"),
        ("weather.home", "get_forecasts", None, "everyday device controls"),
        # entities named inside the data
        ("scene.x", "scene.apply", {"entities": {"lock.front_door": "unlocked"}}, "controls locks"),
        ("script.evening", "turn_on", {"variables": {"target": "cover.garage_door"}}, "garage doors"),
        ("remote.broadlink", "send_command", {"device": "garage", "command": "open"}, "mentions"),
        ("switch.gas_fireplace", "turn_on", None, "may operate"),
        ("button.hub_factory_reset", "press", None, "wipe or reflash"),
    ],
)
def test_gate_closes_bypasses(entity_id, service, data, reason):
    args = {"entity_id": entity_id, "service": service}
    if data is not None:
        args["data"] = data
    p = gate(**args)
    assert p is not None, (entity_id, service, data)
    assert reason in p["body"], p["body"]


@pytest.mark.parametrize(
    "entity_id, service, data",
    [
        ("valve.water_main", "stop_valve", None),
        ("todo.shopping_list", "add_item", {"item": "milk"}),
        ("media_player.kitchen", "join", {"group_members": ["media_player.den"]}),
        ("input_number.bedtime_volume", "set_value", {"value": 3}),
        ("light.porch", "turn_on", {"color_name": "red"}),
        ("all", "media_player.media_pause", None),
        ("vacuum.robo", "start", None),
        ("light.kitchen", "homeassistant.turn_off", None),
        ("media_player.tv", "play_media", {"media_content_id": "song.mp3", "media_content_type": "music"}),
    ],
)
def test_gate_still_lets_routine_run(entity_id, service, data):
    args = {"entity_id": entity_id, "service": service}
    if data is not None:
        args["data"] = data
    assert gate(**args) is None


# ------------------------------------------------------------------ live lookups (friendly names, scene members)
LIVE = STATES + [
    {"entity_id": "switch.shelly1_a1b2c3", "state": "off", "attributes": {"friendly_name": "Garage Door Opener"}},
    {"entity_id": "switch.sonoff_1000ab", "state": "off", "attributes": {"friendly_name": "Fish Tank Pump"}},
    {"entity_id": "switch.sonoff_99", "state": "off", "attributes": {"friendly_name": "Front Gates"}},
    {"entity_id": "scene.arrive_home", "state": "unknown",
     "attributes": {"friendly_name": "Arrive Home", "entity_id": ["light.kitchen", "lock.front_door"]}},
    {"entity_id": "scene.movie_night", "state": "unknown",
     "attributes": {"friendly_name": "Movie Night", "entity_id": ["light.kitchen", "media_player.tv"]}},
    {"entity_id": "scene.porch", "state": "unknown",
     "attributes": {"friendly_name": "Porch", "entity_id": ["light.kitchen", "switch.sonoff_99"]}},
    {"entity_id": "script.welcome", "state": "off", "attributes": {"friendly_name": "Welcome home: disarm alarm"}},
]


def live_gate(handler=None, **args):
    return run(sh.control_gate(ctx_for(handler or FakeHA(states=LIVE)), args))


def test_gate_checks_live_friendly_names():
    p = live_gate(entity_id="switch.shelly1_a1b2c3", service="turn_on")
    assert p and "Garage Door Opener" in p["body"]
    assert live_gate(entity_id="switch.sonoff_1000ab", service="turn_on") is None
    assert "sounds security-related" in live_gate(entity_id="script.welcome", service="turn_on")["body"]
    assert live_gate(entity_id="switch.not_in_ha", service="turn_on") is None  # 404: HA ignores it anyway


def test_gate_checks_scene_members():
    assert "controls locks" in live_gate(entity_id="scene.arrive_home", service="scene.turn_on")["body"]
    assert live_gate(entity_id="scene.movie_night", service="scene.turn_on") is None
    # one level further down: the member's own friendly name
    assert "Front Gates" in live_gate(entity_id="scene.porch", service="scene.turn_on")["body"]


def test_gate_fails_closed_when_lookup_breaks(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    def broken(request):
        return httpx.Response(500, text="oops")

    for handler in (down, broken):
        p = live_gate(handler, entity_id="switch.sonoff_1000ab", service="turn_on")
        assert p and "couldn't check" in p["body"]
    # Not configured / bad token: nothing to approve, the call itself fails with the setup message.
    assert live_gate(FakeHA(states=LIVE), entity_id="switch.sonoff_1000ab", service="turn_on") is None
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", "wrong")
    assert live_gate(FakeHA(states=LIVE), entity_id="switch.sonoff_1000ab", service="turn_on") is None
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", TOKEN)
    many = ", ".join(f"switch.s{i}" for i in range(sh.MAX_LOOKUPS + 1))
    assert "too many devices" in live_gate(entity_id=many, service="turn_on")["body"]


def test_gate_lookup_skipped_for_plain_lights():
    ha = FakeHA(states=LIVE)
    assert live_gate(ha, entity_id="light.kitchen", service="turn_on") is None
    assert ha.requests == []  # lights never need a lookup


# ------------------------------------------------------------------ the tool re-checks when the gate was skipped
def test_control_refuses_security_action_without_gate():
    ha = FakeHA(states=LIVE)
    out = tool("smart_home_control", ctx_for(ha), entity_id="lock.front_door", service="unlock")
    assert "Not done" in out["error"] and "approval card" in out["error"] and "unlocks something" in out["error"]
    out = tool("smart_home_control", ctx_for(ha), entity_id="switch.shelly1_a1b2c3", service="turn_on")
    assert "Garage Door Opener" in out["error"]
    assert not [r for r in ha.requests if r.method == "POST"]  # nothing reached a service


def test_control_runs_after_gate_or_auto_approve():
    ha = FakeHA(states=LIVE)
    ctx = ctx_for(ha)
    args = {"entity_id": "lock.front_door", "service": "unlock", "data": {"code": "4321"}}
    assert run(sh.control_gate(ctx, args)) is not None  # the agent shows this card, the user approves...
    out = tool("smart_home_control", ctx, **args)  # ...then runs the tool
    assert out["status"] == "Called lock.unlock on lock.front_door"
    assert json.loads(ha.requests[-1].content) == {"code": "4321", "entity_id": "lock.front_door"}
    # A different call isn't covered by that approval.
    assert "Not done" in tool("smart_home_control", ctx, entity_id="lock.back_door", service="unlock")["error"]
    # The user put the tool in AUTO_APPROVE.
    auto = make_ctx(http=httpx.AsyncClient(transport=httpx.MockTransport(ha)), auto_approve={"smart_home_control"})
    assert tool("smart_home_control", auto, entity_id="lock.back_door", service="unlock")["status"].startswith("Called")
    # Expired gate record.
    sh._GATE_SEEN.update({k: v - sh.GATE_TTL - 1 for k, v in sh._GATE_SEEN.items()})
    assert "Not done" in tool("smart_home_control", ctx, **args)["error"]


def test_gate_masks_nested_secrets_and_fail_closed_card(monkeypatch):
    p = gate(entity_id="alarm_control_panel.home", service="alarm_disarm", data={"code": "1234", "extra": {"pin": "9"}})
    assert "1234" not in json.dumps(p) and '"9"' not in json.dumps(p)

    def explode(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(sh, "parse_call", explode)
    p = run(sh.control_gate(make_ctx(), {"entity_id": "lock.x", "service": "unlock", "data": {"code": "5555"}}))
    assert p and "couldn't be checked" in p["body"] and "5555" not in json.dumps(p)


# ------------------------------------------------------------------ config hardening
def test_token_and_url_hygiene(monkeypatch):
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", "abc’def")  # smart quote pasted from a doc
    with pytest.raises(ToolError, match="characters"):
        sh._ha_config()
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", TOKEN)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setenv("HOME_ASSISTANT_URL", "http://admin:hunter2@homeassistant.local")
    err = tool("smart_home_devices", ctx_for(down))["error"]
    assert "hunter2" not in err and "port 8123" in err
    monkeypatch.setenv("HOME_ASSISTANT_URL", "http://homeassistant.local:99999")
    err = tool("smart_home_devices", ctx_for(FakeHA()))["error"]
    assert "isn't a valid address" in err


def test_devices_tolerates_odd_argument_types():
    out = tool("smart_home_devices", ctx_for(FakeHA()), query=["kitchen"], domain=None)
    assert "error" not in out
