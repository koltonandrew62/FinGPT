"""The assistant's persona, the per-channel reply style, the no-self-correction rules and sampling settings."""
import json
import re

import httpx
import pytest

from app import agent, config
from app.config import Settings
from app.llm import CHAT_TEMPERATURE, OpenRouter
from app.tools import call_tool
from conftest import make_ctx, run

BASE = "http://openrouter.test/api/v1"
FILM_NAMES = ("jarvis", "j.a.r.v.i.s", "friday", "stark", "iron man", "tony", "alfred", "jeeves", "hal 9000")


def prompt(channel="desktop", **settings):
    return agent.build_system_prompt(Settings(**settings), channel)


def section(text, heading, next_heading):
    return text[text.index(f"\n{heading}"):text.index(f"\n{next_heading}")]


# ------------------------------------------------------------------ persona
def test_persona_is_the_refined_chief_of_staff():
    p = section(prompt(), "PERSONALITY", "CURRENT CONTEXT")
    for trait in ("British", "unflappable", "courteous", "precise and economical", "wit", "sarcasm", "reckless",
                  "Anticipate", "one relevant extra detail", "Never sycophantic", '"as an AI"', "No emojis"):
        assert trait in p, trait
    assert "catchphrases" in p and "fictional character" in p


def test_persona_uses_the_configured_name_and_form_of_address():
    p = prompt(assistant_name="Nova", address_as="sir", user_name="Kolton")
    assert p.startswith("You are Nova,")
    examples = p[p.index("TONE EXAMPLES"):p.index("\nCURRENT CONTEXT")]
    replies = [line for line in examples.splitlines() if line.startswith("Nova: ")]
    assert len(replies) == 4 and examples.count("User: ") == 4
    assert 'Address the user as "sir" naturally, roughly once per reply' in p
    for reply in replies:
        assert reply.count("sir") <= 1  # roughly once per reply, never in every sentence
    assert sum("sir" in r for r in replies) >= 2
    assert "ATLAS:" not in p and "Atlas:" not in p  # no other name is hard-coded into the examples


def test_persona_without_a_form_of_address():
    p = prompt(assistant_name="Nova")
    examples = p[p.index("TONE EXAMPLES"):p.index("\nCURRENT CONTEXT")]
    assert ", sir" not in examples and ", ," not in examples and " ;" not in examples
    assert "You don't know the user's name yet" in p
    assert "Yes. It's a 22-minute walk" in examples  # the sentence still reads cleanly
    p = prompt(user_name="Kolton")  # no USER_ADDRESS_AS: the user's name stands in
    assert 'Address the user as "Kolton"' in p


def test_prompt_is_original_and_emoji_free():
    for channel in agent.CHANNELS:
        p = prompt(channel, assistant_name="Nova", address_as="sir").lower()
        for name in FILM_NAMES:
            assert not re.search(rf"\b{re.escape(name)}\b", p), (channel, name)
        assert not re.search("[\U0001F000-\U0001FAFF☀-➿]", p), channel


def test_tone_examples_model_tool_first_answers():
    p = prompt(assistant_name="Nova", address_as="sir")
    examples = p[p.index("TONE EXAMPLES"):p.index("\nCURRENT CONTEXT")]
    replies = [line[len("Nova: "):] for line in examples.splitlines() if line.startswith("Nova: ")]
    for reply in replies:
        assert not re.search(r"\b(let me|one moment|actually|wait|i mean|correction)\b", reply, re.I), reply
        assert "!" not in reply and "[" not in reply
    assert "run first" in examples and "never reuse their wording" in examples


# ------------------------------------------------------------------ no self-correction
def test_no_self_correction_rules():
    p = prompt()
    rules = section(p, "ONE ANSWER, GIVEN ONCE", "KNOWLEDGE & ACCURACY")
    assert "BEFORE you write anything" in rules
    assert "no text before it" in rules and '"Let me check"' in rules
    assert "one final, settled answer" in rules
    for word in ('"actually"', '"wait"', '"correction"', '"I mean"'):
        assert word in rules, word
    assert "think aloud" in rules and "say so once, plainly" in rules
    for channel in agent.CHANNELS:  # the rules hold on every channel
        assert "\nONE ANSWER, GIVEN ONCE" in prompt(channel)


# ------------------------------------------------------------------ channel style
def test_desktop_style_is_unchanged():
    style = section(prompt(), "HOW YOU RESPOND", "ONE ANSWER, GIVEN ONCE")
    assert "Replies are usually spoken aloud. For simple requests answer in 1-3 natural sentences, no markdown." in style
    assert "tight markdown" in style and "Channel:" not in style


def test_voice_style_is_spoken():
    style = section(prompt("voice"), "HOW YOU RESPOND", "ONE ANSWER, GIVEN ONCE")
    assert "Channel: voice" in style
    assert "No markdown, bullets, headings, tables, URLs, file paths, emojis" in style
    assert "Say numbers and times as a person would" in style and "twenty past three" in style
    assert "1-3 sentences unless the user asks for more" in style and "flowing speech" in style
    assert "tight markdown" not in style and "phone call" not in style


def test_phone_style_is_spoken_and_says_it_is_a_call():
    style = section(prompt("phone"), "HOW YOU RESPOND", "ONE ANSWER, GIVEN ONCE")
    assert "You are on a phone call with the user" in style
    assert "No markdown" in style and "1-3 sentences" in style and "flowing speech" in style
    assert "cannot be done by phone" in style and "desktop" in style
    assert "tight markdown" not in style


def test_sms_style_is_short_plain_text():
    style = section(prompt("sms"), "HOW YOU RESPOND", "ONE ANSWER, GIVEN ONCE")
    assert "text message (SMS)" in style
    assert "Plain text only: no markdown" in style and "emojis" in style
    assert "At most 320 characters unless the user asks for more" in style
    assert "cannot be done by text message" in style
    assert "Speak, don't write" not in style and "tight markdown" not in style


def test_every_channel_keeps_the_required_sections():
    for channel in agent.CHANNELS:
        p = prompt(channel)
        for heading in ("PERSONALITY", "CURRENT CONTEXT", "HOW YOU RESPOND", "KNOWLEDGE & ACCURACY", "LIVE DATA",
                        "TOOLS & ACTIONS", "WEB BROWSER & RESERVATIONS", "LONG-TERM MEMORY ABOUT THE USER"):
            assert f"\n{heading}" in p, (channel, heading)
        assert len(p) < 16000, channel  # the prompt goes out with every model call


def test_build_system_prompt_rejects_unknown_channels():
    with pytest.raises(ValueError):
        prompt("carrier-pigeon")


# ------------------------------------------------------------------ sampling
def _sse(*chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def test_chat_runs_at_a_low_temperature():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return _sse({"choices": [{"delta": {"content": "ok"}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = OpenRouter(Settings(api_key="k", base_url=BASE), http)
            [ev async for ev in llm.stream_chat([{"role": "user", "content": "hi"}], model="m")]
            [ev async for ev in llm.stream_chat([{"role": "user", "content": "hi"}], model="m", temperature=0.9)]

    run(go())
    assert CHAT_TEMPERATURE == 0.3
    assert payloads[0]["temperature"] == 0.3 and payloads[1]["temperature"] == 0.9


def test_one_shot_calls_default_to_factual_and_callers_can_override():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = OpenRouter(Settings(api_key="k", base_url=BASE), http)
            await llm.complete([{"role": "user", "content": "describe"}], model="m")
            await llm.complete([{"role": "user", "content": "q"}], model="m", temperature=0.7)

    run(go())
    assert payloads[0]["temperature"] <= 0.2 and payloads[1]["temperature"] == 0.7


def test_research_sub_call_stays_factual_and_free_of_the_persona():
    payloads = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "google/gemini-2.5-flash", "supported_parameters": ["tools"]}]})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "The Fed held rates.", "annotations": []}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            s = Settings(api_key="k", base_url=BASE, address_as="sir")
            llm = OpenRouter(s, http)
            ctx = make_ctx(http=http, llm=llm)
            return json.loads(await call_tool("web_search", {"query": "fed decision"}, ctx))

    out = run(go())
    assert out["answer"] == "The Fed held rates."
    (payload,) = payloads
    assert payload["temperature"] <= 0.2 and "stream" not in payload and "tools" not in payload
    system = payload["messages"][0]["content"]
    assert payload["messages"][0]["role"] == "system" and "research engine" in system and "verifiable facts" in system
    for persona_word in ("PERSONALITY", "butler", "sarcasm", "wit", "sir"):
        assert not re.search(rf"\b{persona_word}\b", system), persona_word
