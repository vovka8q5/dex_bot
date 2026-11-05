import pytest

from dexbot.decisions import DecisionParseError, extract_json_object, parse_team_decision


def test_parse_fenced_and_surrounding_prose():
    text = """Here is the call:
```json
{"symbol":"BTC/USDT","action":"WAIT","confidence":0.4,"reasoning":"no edge","dissent":"volume"}
```
thanks
"""
    decision = parse_team_decision(text)
    assert decision.action == "wait"
    assert decision.confidence == pytest.approx(0.4)


def test_parse_nested_object_and_buy_prices():
    payload = extract_json_object(
        'note {"action":"skip","confidence":0.1,"reasoning":"x","extra":{"a":1}} trailing'
    )
    assert payload["extra"] == {"a": 1}
    decision = parse_team_decision(
        '{"action":"BUY","confidence":0.8,"entry":100,"stop_loss":95,"take_profit":110,'
        '"reasoning":"planned","dissent":""}'
    )
    assert decision.action == "buy"
    assert decision.stop_loss == 95


def test_reject_bad_enum_and_buy_without_prices():
    with pytest.raises(DecisionParseError):
        parse_team_decision('{"action":"long","confidence":0.5,"reasoning":"no"}')
    with pytest.raises(DecisionParseError):
        parse_team_decision(
            '{"action":"buy","confidence":0.5,"reasoning":"missing prices"}'
        )
    with pytest.raises(DecisionParseError):
        parse_team_decision(
            '{"action":"buy","confidence":0.5,"entry":100,"stop_loss":101,"take_profit":110,"reasoning":"bad"}'
        )
    with pytest.raises(DecisionParseError):
        parse_team_decision("no json here")
