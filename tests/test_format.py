"""The builders a worker author writes with.

``to_round`` / ``parse`` return these; the point is that the ergonomic
constructors (Round, Text, ToolUse, …) produce the exact wire shape the
sink expects, and that the sharp edges — empty blocks, dict inputs,
str content — behave so a worker body stays a one-liner per case.
"""
from collectbase.format import Block, Code, Round, Text, Thinking, ToolResult, ToolUse


class TestContentBlocks:

    def test_empty_text_blocks_vanish(self):
        """So `[b for b in map(_block, raw) if b]` drops them cleanly."""
        assert Text("") is None
        assert Text(None) is None
        assert Thinking("") is None
        assert Code("") is None

    def test_text_and_thinking_and_code_carry_their_fields(self):
        assert Text("hi").text == "hi"
        assert Thinking("why").thinking == "why"
        c = Code("x=1", language="python")
        assert (c.type, c.language) == ("code", "python")

    def test_tool_use_serializes_dict_input_and_projects_text(self):
        """tool_use stays typed, but carries a text projection for FTS."""
        b = ToolUse("Read", {"file": "sync.py"})
        assert b.type == "tool_use"
        assert b.input == '{"file": "sync.py"}'
        assert b.text == '[Read] {"file": "sync.py"}'

    def test_tool_result_stringifies_structured_output(self):
        assert ToolResult(["a", "b"]).text == '["a", "b"]'
        assert ToolResult("plain").text == "plain"

    def test_block_is_an_escape_hatch_that_keeps_unknown_fields(self):
        b = Block("annotation", ref="c1", text="see card")
        assert (b.type, b.ref, b.text) == ("annotation", "c1", "see card")


class TestRound:

    def test_a_string_becomes_a_single_text_block(self):
        r = Round(id="u1", role="human", content="hi there")
        assert r.round_id == "u1"
        assert r.speaker == "human"  # defaults to role
        assert [b.text for b in r.content] == ["hi there"]

    def test_none_blocks_are_dropped_from_content(self):
        r = Round(id="a1", role="assistant", content=[Text("hi"), Text(""), ToolUse("Read")])
        assert [b.type for b in r.content] == ["text", "tool_use"]

    def test_index_is_never_authored(self):
        """The server assigns index; a worker must not (and can't) set it."""
        assert not hasattr(Round(id="u1", role="human", content="x"), "index")
