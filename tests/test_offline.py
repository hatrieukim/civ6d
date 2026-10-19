"""Tests that need no running game: parsing, Lua encoding, safety guards."""

import pytest

from civ6d import actions as A
from civ6d import rules
from civ6d.conn import _text
from civ6d.core import BAD_RESULT, BLOCKED_OPS, Civ6D, parse_event


def test_parse_event():
    ev = parse_event("@@EV|12|34|unit_seen|owner=Barbarian|unit=UNIT_WARRIOR|x=-1|near_city=Ur")
    assert ev == {"seq": 12, "turn": 34, "kind": "unit_seen", "owner": "Barbarian",
                  "unit": "UNIT_WARRIOR", "x": -1, "near_city": "Ur"}


def test_event_replay_is_dropped():
    d = Civ6D()
    d._on_event("@@EV|1|5|my_turn")
    d._on_event("@@EV|1|5|my_turn")  # AG.since replays what we already have
    d._on_event("@@EV|2|5|turn_end")
    assert [e["kind"] for e in d.events] == ["my_turn", "turn_end"]
    assert [e["seq"] for e in d.events] == [1, 2]


@pytest.mark.parametrize("text,bad", [
    ("Moved to (3,4)", False),
    ("Estimate: 30 dmg\nError: target not visible", True),
    ("CANNOT found city here", True),
    ("Failed to set research", True),
    ("Errand runner trained", False),
])
def test_bad_result(text, bad):
    assert bool(BAD_RESULT.search(text)) is bad


def test_print_payload():
    assert _text("O\x00InGame: hello: world") == "hello: world"
    assert _text("ERR:boom") is None


def test_lua_table_escapes_strings():
    assert A.lua_table({"UNIT_TYPE": 'X"]] os.exit()', "N": 3, "B": True}) == \
        '{["UNIT_TYPE"]="X\\"]] os.exit()",["N"]=3,["B"]=true}'


def test_action_names_are_identifiers():
    assert A._ident("unitoperation_move_to") == "UNITOPERATION_MOVE_TO"
    with pytest.raises(ValueError):
        A._ident("MOVE_TO); Players[0]:SetGold(9999")


def test_parse_listing():
    out = A.parse_listing([
        "UNIT|UNIT_ARCHER|10,12|hp=100",
        "A|op|UNITOPERATION_RANGE_ATTACK|1|true|targets(1)=11,12|",
        "A|cmd|UNITCOMMAND_PROMOTE|0|false||not enough experience",
    ])
    assert out["subject"] == "UNIT_ARCHER" and out["at"] == "10,12"
    assert out["actions"][0] == {"kind": "op", "action": "UNITOPERATION_RANGE_ATTACK", "can_now": True,
                                 "needs_target": True, "info": "targets(1)=11,12"}
    assert out["actions"][1]["blocked_because"] == "not enough experience"


@pytest.mark.parametrize("sql", ["DELETE FROM Units", "UPDATE Units SET Cost=1",
                                 "SELECT 1; DROP TABLE Units", "ATTACH 'x' AS y"])
def test_rules_are_read_only(sql):
    with pytest.raises(ValueError):
        rules.query(sql)


def test_cheating_ops_are_hidden():
    ops = Civ6D().ops()
    assert "promote_unit" in BLOCKED_OPS
    assert not set(BLOCKED_OPS) & set(ops)
    assert "unit_act" in ops
