"""The predefined chats: the file loader, the route that serves it, and its wiring in ``main``."""

from pathlib import Path

import pytest

from advisor_agent.web import __main__ as web_main
from advisor_agent.web.chat import MAX_PROMPT_CHARS
from advisor_agent.web.predefined import MAX_CHATS, MAX_FILE_BYTES, MAX_PROBLEMS, load_predefined_chats

TWO = '''
[[chat]]
title = "First"
prompt = "Do the first thing."

[[chat]]
title = "Second"
prompt = """
Line one.
Line two.
"""
'''


def write(tmp_path: Path, text: str | bytes, name: str = "chats.toml") -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def only_problem(result: dict) -> str:
    assert result["chats"] == []
    assert len(result["problems"]) == 1
    return result["problems"][0]


# -- the loader ------------------------------------------------------------------------------


def test_a_valid_file_gives_its_chats_in_order(tmp_path):
    result = load_predefined_chats(write(tmp_path, TWO))

    assert result["problems"] == []
    assert [chat["title"] for chat in result["chats"]] == ["First", "Second"]
    assert result["chats"][0]["prompt"] == "Do the first thing."


def test_a_multi_line_prompt_keeps_its_line_breaks_but_not_the_padding(tmp_path):
    result = load_predefined_chats(write(tmp_path, TWO))

    assert result["chats"][1]["prompt"] == "Line one.\nLine two."


@pytest.mark.parametrize("path_kind", ["none", "missing"])
def test_no_file_means_no_chats_and_no_problem(tmp_path, path_kind):
    path = None if path_kind == "none" else tmp_path / "chats.toml"

    assert load_predefined_chats(path) == {"chats": [], "problems": []}


def test_an_empty_file_says_it_has_no_chats(tmp_path):
    problem = only_problem(load_predefined_chats(write(tmp_path, "# only a comment\n")))

    assert "no [[chat]] entries" in problem


def test_invalid_toml_is_one_problem_that_names_the_line(tmp_path):
    problem = only_problem(load_predefined_chats(write(tmp_path, '[[chat]]\ntitle = "open\n')))

    assert problem.startswith("chats.toml: is not valid TOML")
    assert "line" in problem


def test_a_file_that_is_not_utf8_is_one_problem(tmp_path):
    problem = only_problem(load_predefined_chats(write(tmp_path, b'[[chat]]\ntitle = "\xff\xfe"\n')))

    assert "not UTF-8" in problem


def test_a_directory_in_place_of_the_file_is_one_problem(tmp_path):
    (tmp_path / "chats.toml").mkdir()

    problem = only_problem(load_predefined_chats(tmp_path / "chats.toml"))

    assert "cannot be read" in problem


def test_a_file_that_is_too_large_is_one_problem_and_is_not_parsed(tmp_path):
    big = "# " + "x" * MAX_FILE_BYTES + "\n"

    problem = only_problem(load_predefined_chats(write(tmp_path, big)))

    assert "too large" in problem


@pytest.mark.parametrize(
    "entry, expected",
    [
        ('title = "T"', "chat 1 has no prompt"),
        ('prompt = "P"', "chat 1 has no title"),
        ('title = "T"\nprompt = "   "', "chat 1 has no prompt"),
        ('title = "  "\nprompt = "P"', "chat 1 has no title"),
        ('title = 5\nprompt = "P"', "chat 1 has no title"),
        ('title = "T"\nprompt = ["a", "b"]', "chat 1 has no prompt"),
        (f'title = "T"\nprompt = "{"x" * (MAX_PROMPT_CHARS + 1)}"', "longer than"),
    ],
)
def test_a_bad_entry_is_skipped_and_named_while_the_others_still_load(tmp_path, entry, expected):
    text = f'[[chat]]\n{entry}\n\n[[chat]]\ntitle = "Good"\nprompt = "Fine."\n'

    result = load_predefined_chats(write(tmp_path, text))

    assert [chat["title"] for chat in result["chats"]] == ["Good"]
    assert len(result["problems"]) == 1 and expected in result["problems"][0]
    assert result["problems"][0].startswith("chats.toml: ")


def test_a_prompt_at_exactly_the_limit_is_accepted(tmp_path):
    result = load_predefined_chats(write(tmp_path, f'[[chat]]\ntitle = "T"\nprompt = "{"x" * MAX_PROMPT_CHARS}"\n'))

    assert result["problems"] == [] and len(result["chats"][0]["prompt"]) == MAX_PROMPT_CHARS


def test_chat_written_as_a_plain_value_is_explained(tmp_path):
    problem = only_problem(load_predefined_chats(write(tmp_path, "chat = 5\n")))

    assert "[[chat]]" in problem


def test_an_entry_that_is_not_a_table_is_skipped(tmp_path):
    result = load_predefined_chats(write(tmp_path, 'chat = [5, {title = "T", prompt = "P"}]\n'))

    assert [chat["title"] for chat in result["chats"]] == ["T"]
    assert "chat 1 is not a [[chat]] table" in result["problems"][0]


def test_unknown_keys_are_ignored(tmp_path):
    result = load_predefined_chats(write(tmp_path, '[[chat]]\ntitle = "T"\nprompt = "P"\nhint = "later"\n'))

    assert result == {"chats": [{"title": "T", "prompt": "P"}], "problems": []}


def test_a_title_is_one_clean_line_of_limited_length(tmp_path):
    long_title = "y" * 200
    text = f'[[chat]]\ntitle = "Bad\\u0007  title\\nsplit"\nprompt = "P"\n\n[[chat]]\ntitle = "{long_title}"\nprompt = "P"\n'

    titles = [chat["title"] for chat in load_predefined_chats(write(tmp_path, text))["chats"]]

    assert titles[0] == "Bad title split"
    assert len(titles[1]) == 80 and titles[1].endswith("...")


def test_only_the_first_chats_are_used(tmp_path):
    text = "".join(f'[[chat]]\ntitle = "Chat {n}"\nprompt = "P"\n\n' for n in range(MAX_CHATS + 10))

    result = load_predefined_chats(write(tmp_path, text))

    assert len(result["chats"]) == MAX_CHATS
    assert result["chats"][-1]["title"] == f"Chat {MAX_CHATS - 1}"
    assert result["problems"] == [f"chats.toml: only the first {MAX_CHATS} chats are used."]


def test_many_problems_are_cut_to_a_few_lines(tmp_path):
    bad_entries = MAX_PROBLEMS + 5
    text = "".join('[[chat]]\ntitle = "T"\n\n' for _ in range(bad_entries))

    result = load_predefined_chats(write(tmp_path, text))

    assert result["chats"] == []
    # The first few problems are named; the rest are counted in one last line.
    assert len(result["problems"]) == MAX_PROBLEMS
    assert result["problems"][0] == "chats.toml: chat 1 has no prompt."
    assert result["problems"][-1] == f"chats.toml: and {bad_entries - (MAX_PROBLEMS - 1)} more problems."


@pytest.mark.parametrize(
    "content",
    [
        b'[[chat]]\ntitle = "\xff"\n',  # not UTF-8
        "not toml at all [\n",
        "[[chat]]\ntitle = 1\n",
        "chat = 5\n",
        "",
    ],
)
def test_a_problem_never_contains_the_path_of_the_file(tmp_path, content):
    deep = tmp_path / "some" / "private" / "place"
    deep.mkdir(parents=True)

    result = load_predefined_chats(write(deep, content))

    assert result["problems"]
    for problem in result["problems"]:
        assert str(tmp_path) not in problem and "private" not in problem


def test_the_chats_file_that_ships_with_the_repository_is_usable():
    result = load_predefined_chats(web_main.CHATS_FILE)

    assert result["problems"] == []
    assert len(result["chats"]) >= 1
    assert all(chat["prompt"] and chat["title"] for chat in result["chats"])


# -- the route -------------------------------------------------------------------------------


def test_the_route_serves_the_chats(web, tmp_path):
    path = write(tmp_path, TWO)
    with web(chats_file=path) as app:
        response = app.get("/api/predefined")

    assert response.status_code == 200
    body = response.json()
    assert [chat["title"] for chat in body["chats"]] == ["First", "Second"]
    assert body["problems"] == []


def test_an_edit_shows_on_the_next_request_without_a_restart(web, tmp_path):
    path = write(tmp_path, TWO)
    with web(chats_file=path) as app:
        before = app.get("/api/predefined").json()
        write(tmp_path, '[[chat]]\ntitle = "Changed"\nprompt = "New text."\n')
        after = app.get("/api/predefined").json()

    assert len(before["chats"]) == 2
    assert after["chats"] == [{"title": "Changed", "prompt": "New text."}]


def test_without_a_chats_file_the_route_answers_with_an_empty_list(web):
    with web() as app:
        response = app.get("/api/predefined")

    assert response.status_code == 200
    assert response.json() == {"chats": [], "problems": []}


def test_a_broken_file_is_a_200_with_a_problem_not_a_server_error(web, tmp_path):
    path = write(tmp_path, "[[chat\n")
    with web(chats_file=path) as app:
        response = app.get("/api/predefined")

    assert response.status_code == 200
    body = response.json()
    assert body["chats"] == [] and len(body["problems"]) == 1
    assert str(tmp_path) not in body["problems"][0]


def test_a_file_that_goes_away_while_the_server_runs_is_just_empty(web, tmp_path):
    path = write(tmp_path, TWO)
    with web(chats_file=path) as app:
        path.unlink()
        response = app.get("/api/predefined")

    assert response.status_code == 200 and response.json() == {"chats": [], "problems": []}


# -- main ------------------------------------------------------------------------------------


def test_main_hands_the_repository_file_to_the_server(monkeypatch):
    """The one wiring step no other test sees: if ``main`` forgot it, the feature would be silently off."""
    seen = {}
    monkeypatch.setattr(web_main, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_main, "build_server", lambda settings, **kwargs: seen.update(settings=settings))
    monkeypatch.setattr(web_main, "run_server", lambda server: None)

    web_main.main(["--port", "8799"])

    assert seen["settings"].chats_file == web_main.CHATS_FILE
    assert web_main.CHATS_FILE.name == "chats.toml" and web_main.CHATS_FILE.parent == web_main.ENV_FILE.parent
