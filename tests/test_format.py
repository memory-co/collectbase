from collectbase.format import (
    Block,
    Code,
    Round,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)


def test_text_empty_is_none():
    assert Text("") is None
    assert Text(None) is None
    assert Text("hi").type == "text"
    assert Text("hi").text == "hi"


def test_thinking_and_code():
    assert Thinking("") is None
    assert Thinking("reason").thinking == "reason"
    c = Code("x=1", language="python")
    assert c.type == "code" and c.language == "python"


def test_tool_use_serializes_dict_and_adds_text():
    b = ToolUse("Read", {"file": "sync.py"})
    assert b.type == "tool_use"
    assert b.input == '{"file": "sync.py"}'
    assert b.text == '[Read] {"file": "sync.py"}'


def test_tool_result_stringifies():
    assert ToolResult(["a", "b"]).text == '["a", "b"]'
    assert ToolResult("plain").text == "plain"


def test_block_escape_hatch_keeps_extra_fields():
    b = Block("annotation", ref="c1", text="see card")
    assert b.type == "annotation"
    assert b.ref == "c1"
    assert b.text == "see card"


def test_round_from_str_wraps_in_text_block():
    r = Round(id="u1", role="human", content="hi there")
    assert r.round_id == "u1"
    assert r.role == "human"
    assert r.speaker == "human"  # defaults to role
    assert len(r.content) == 1 and r.content[0].text == "hi there"


def test_round_drops_none_blocks():
    r = Round(id="a1", role="assistant", content=[Text("hi"), Text(""), ToolUse("Read")])
    assert [b.type for b in r.content] == ["text", "tool_use"]


def test_round_has_no_index_field():
    r = Round(id="u1", role="human", content="x")
    assert not hasattr(r, "index")
