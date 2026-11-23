from pathlib import Path

from maverick.guide import load_guide
from maverick.state import Session, Verdict, clip

GUIDE = load_guide(Path(__file__).resolve().parents[2] / "guides" / "lego-car.json")


def new():
    return Session(GUIDE)


def test_guide_steps_fit_the_display():
    for step in GUIDE.steps:
        assert len(step.say.split()) <= 8, step.say
        assert step.check


def test_starts_on_step_one():
    screen = new().step_screen()
    assert screen.top == "STEP 1 / 6" and not screen.warn and screen.progress == 0


def test_done_advances_and_marks_previous_step():
    s = new()
    screen = s.apply_verdict(Verdict(status="done"), for_index=0)
    assert s.index == 1 and screen.top == "STEP 2 / 6" and screen.big.startswith("✓ ")


def test_not_yet_changes_nothing():
    s = new()
    assert s.apply_verdict(Verdict(status="not_yet"), for_index=0) is None and s.index == 0


def test_mistake_warns_but_stays_on_step():
    s = new()
    screen = s.apply_verdict(Verdict(status="mistake", message="Wrong color plate"), for_index=0)
    assert screen.warn and screen.big == "! Wrong color plate" and s.index == 0


def test_stale_verdict_is_ignored():
    s = new()
    s.command("next")
    assert s.apply_verdict(Verdict(status="done"), for_index=0) is None and s.index == 1


def test_voice_commands_work_without_camera():
    s = new()
    s.command("next"); s.command("next")
    assert s.index == 2
    s.command("back")
    assert s.index == 1
    assert s.command("more").sub == GUIDE.steps[1].detail
    assert s.command("nonsense") is None


def test_finishing_and_restart():
    s = new()
    for _ in range(6):
        screen = s.command("next")
    assert s.finished and screen.big == "All done" and screen.progress == 1.0
    assert s.apply_verdict(Verdict(status="done"), for_index=5) is None
    assert s.command("restart").top == "STEP 1 / 6" and not s.finished


def test_back_never_goes_below_first_step():
    s = new()
    s.command("back")
    assert s.index == 0


def test_long_text_is_clipped():
    assert clip("one two three four five six seven eight nine ten") == "one two three four five six seven eight…"
