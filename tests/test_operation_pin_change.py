"""Changing the car's operation PIN without unlinking the account (discussion #225, @alextchao).

*"Is there a way to update the car's pin on Mate after changing it on the car?"* — there was not.
The PIN was written in exactly two places: the setup wizard saved it, and Logout cleared it. So four
digits changed on the car meant signing out of the Leapmotor account and walking the whole wizard
again. Nothing was lost doing that — history is keyed by VIN — but nothing about it is obvious, and
Logout is a frightening button to press for a typo.

Typed twice, for the same reason as the access password (#214 @rop12770): a PIN stored with a typo
does not fail here, it fails at the CAR, later, with an error that names no digit.

The PIN is set on the car, so the form always stores one car's: the car of the Vehicle card, or
the one picked from the second car on.
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SETTINGS = (ROOT / "web" / "templates" / "settings.html").read_text()
LOCALES = sorted((ROOT / "web" / "locales").glob("*.json"))


def _form() -> str:
    """The PIN form's own block in the settings template."""
    assert 'hx-post="api/settings/pin"' in SETTINGS, "the form is not on the page"
    return SETTINGS.split('hx-post="api/settings/pin"', 1)[1].split("</form>", 1)[0]


# ── the route ────────────────────────────────────────────────────────────────

A, B = "LFZB10AAAAAAAAAA1", "LFZC10BBBBBBBBBB2"


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A configured install and the web app on it; `cars` adds the cars the account has."""
    pytest.importorskip("fastapi", reason="web.main needs fastapi")
    import db as D
    import db_reader
    import main
    from starlette.testclient import TestClient
    for var in ("MATE_AUTH_PASSWORD", "SUPERVISOR_TOKEN", "HASSIO_TOKEN", "LEAPMOTOR_PIN"):
        monkeypatch.delenv(var, raising=False)
    poller = D.Database(str(tmp_path / "p.db"))
    monkeypatch.setattr(db_reader, "DB_PATH", str(tmp_path / "p.db"))

    def cars(*vins):
        main.apply_setup_vehicles([{"vin": v, "car_type": "B10" if v == A else "C10",
                                    "battery": "67.1", "is_reev": "0", "pin": "1111"} for v in vins])
        db_reader.set_setting("setup_complete", "1")
        db_reader.set_setting("leapmotor_user", "owner@example.com")
        return TestClient(main.app), poller
    return cars


def test_with_two_cars_one_car_s_pin_leaves_the_other(web):
    client, _ = web(A, B)
    import db_reader
    assert client.post("/api/settings/pin", data={"pin": "2222", "pin2": "2222", "vin": B}).status_code == 204
    assert db_reader.get_operate_pin(B) == "2222"
    assert db_reader.get_operate_pin(A) == "1111"


@pytest.mark.parametrize("form,why", [
    ({"pin": "2222", "pin2": "2223"}, "mismatch"),
    ({"pin": "", "pin2": ""}, "empty"),
    ({"pin": "2222", "pin2": "2222", "vin": "LFZGHOST000000000"}, "unknown vin"),
    ({"pin": "2222", "pin2": "2222", "vin": ""}, "unknown vin"),
], ids=["mismatch", "empty", "unknown-car", "no-car"])
def test_a_bad_form_is_refused_and_nothing_is_stored(web, form, why):
    """`required` in the form is a convenience; the check that matters is the server's."""
    client, _ = web(A)
    import db_reader
    r = client.post("/api/settings/pin", data={"vin": A, **form})
    assert (r.status_code, r.text) == (422, why)
    assert db_reader.get_operate_pin(A) == "1111"


def test_logout_clears_every_pin(web):
    client, _ = web(A, B)
    import db_reader
    db_reader.set_secret("leapmotor_pin", "2222")    # typed in a wizard that found no car
    assert client.post("/api/account/logout").status_code == 204
    assert db_reader.get_operate_pin(A) == db_reader.get_operate_pin(B) == ""
    assert db_reader.get_secret("leapmotor_pin") == ""


def test_one_car_s_form_stores_the_pin_commands_use(web):
    """The one-car form used to post no car, so it stored an install-wide PIN that the wizard's own
    PIN for the car outranked: the commands kept the old PIN."""
    client, poller = web(A)
    import db_reader
    page = client.get("/settings").text
    form = page.split('hx-post="api/settings/pin"', 1)[1].split("</form>", 1)[0]
    assert "Operation PIN of this car · B10 · AAAAA1" in form
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form))
    assert client.post("/api/settings/pin", data={**fields, "pin": "2222", "pin2": "2222"}).status_code == 204
    assert db_reader.get_operate_pin(A) == poller.get_operate_pin(A) == "2222"


def test_two_cars_pick_one_and_there_is_no_all_cars(web):
    client, _ = web(A, B)
    import db_reader
    db_reader.set_active_vehicle(B)
    page = client.get("/settings").text
    form = page.split('hx-post="api/settings/pin"', 1)[1].split("</form>", 1)[0]
    options = re.findall(r'<option value="([^"]*)"( selected)?', form)
    assert options == [(A, ""), (B, " selected")], "only cars, the one on screen chosen"


# ── the form ─────────────────────────────────────────────────────────────────

def test_it_sits_under_the_account_email():
    """Silvio's call: under the Email account row, where someone looking for their login details
    already is — not in a section of its own."""
    email = SETTINGS.index("t('setup_email')")
    form = SETTINGS.index('hx-post="api/settings/pin"')
    logout = SETTINGS.index('hx-post="api/account/logout"')
    assert email < form < logout, "the PIN form belongs between the account email and Logout"


def test_both_boxes_are_there_and_both_can_be_revealed():
    """The eye is the point of typing it twice: two hidden boxes you cannot read are two chances to
    make the same typo. Anchored to the input tags, not to the word 'pin' — that word is also in the
    comment above them."""
    form = _form()
    assert len(re.findall(r'<input[^>]*\sname="pin"', form)) == 1
    assert len(re.findall(r'<input[^>]*\sname="pin2"', form)) == 1
    assert form.count("lmPwReveal(this)") == 2, "each box needs its own eye"
    assert form.count("ico_eye()") == 2 and form.count("ico_eye_off()") == 2


def test_the_two_boxes_are_matched_as_you_type():
    form = _form()
    assert 'oninput="lmPwMatch(this.form.pin2)"' in form
    assert 'data-mismatch="{{ t(\'auth_mismatch\') }}"' in form


def test_the_boxes_are_not_password_managers_business():
    """`autocomplete="off"` on both: a browser offering to save four digits as a login password, or
    filling them from one, is how the wrong PIN gets in without anyone typing it."""
    form = _form()
    assert form.count('autocomplete="off"') == 2


def test_it_keeps_the_wizard_s_own_constraints():
    form = _form()
    assert form.count('maxlength="4"') == 2
    assert form.count('inputmode="numeric"') == 2


# ── the words ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", LOCALES, ids=lambda p: p.stem)
def test_the_hint_exists_in_every_language(path):
    d = json.loads(path.read_text(encoding="utf-8"))["translations"]
    assert d.get("account_pin_hint"), f"{path.stem} is missing account_pin_hint"


@pytest.mark.parametrize("path", LOCALES, ids=lambda p: p.stem)
def test_the_words_it_borrows_all_exist(path):
    """The form reuses six strings that were already written rather than inventing new ones — but a
    reused key is only free while it is still there in every language."""
    d = json.loads(path.read_text(encoding="utf-8"))["translations"]
    for key in ("setup_pin", "setup_pin_ph", "auth_reveal", "auth_mismatch", "save"):
        assert d.get(key), f"{path.stem} is missing the borrowed key {key}"
    assert d.get("account_pin_confirm"), f"{path.stem} is missing account_pin_confirm"


def test_the_confirm_label_is_not_the_password_one():
    """Reusing a string is free until it carries grammar. `auth_confirm` was written for «la
    password», and four of the seven languages agree with it: Italian said «Ripetila», Dutch
    «Herhaal het», Polish «Powtórz je», Portuguese «Repete-a» — all beside a masculine PIN. Only
    visible by looking at the page; every test was green."""
    assert "t('account_pin_confirm')" in _form()
    assert "t('auth_confirm')" not in _form()


def test_the_hint_says_the_account_need_not_be_unlinked():
    """The whole point of the question. If the sentence does not say it, the person still presses
    Logout — which is exactly what they were doing before."""
    d = json.loads((ROOT / "web" / "locales" / "en.json").read_text(encoding="utf-8"))["translations"]
    assert "unlink" in d["account_pin_hint"].lower()
