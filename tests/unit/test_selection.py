from jj_stack.stack.selection import parse_comma_separated_flag_values


def test_parse_comma_separated_flag_values_dedupes_keeping_first_occurrence_order() -> None:
    assert parse_comma_separated_flag_values(["alice,bob", "carol,bob", "alice"]) == [
        "alice",
        "bob",
        "carol",
    ]
