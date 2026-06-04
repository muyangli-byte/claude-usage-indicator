"""Pure-logic tests for the Claude Usage Indicator core.

These cover the side-effect-free functions (parsing, formatting, validation,
version compare, the data model and store) that have no GTK/network/keyring
dependency, so they run under a bare Python with only pytest installed.

After the modular refactor the pure logic lives in cui.api / cui.model /
cui.credentials; the assertions are unchanged from when they were written
against the single-file build, which is what proves the refactor preserved
behavior.
"""
import pytest

from cui import api, credentials, model


# ---------------- version compare ----------------
def test_ver_tuple():
    assert api._ver_tuple("2.10.1") == (2, 10, 1)
    assert api._ver_tuple("0.0.0") == (0, 0, 0)
    assert api._ver_tuple("") == ()
    assert api._ver_tuple(None) == ()
    assert api._ver_tuple("1.2.x") == ()  # non-numeric -> ()


def test_remote_is_newer(monkeypatch):
    monkeypatch.setattr(api, "__version__", "2.10.1")
    assert api.remote_is_newer("2.10.2") is True
    assert api.remote_is_newer("2.11.0") is True
    assert api.remote_is_newer("3.0.0") is True
    assert api.remote_is_newer("2.10.1") is False
    assert api.remote_is_newer("2.10.0") is False
    assert api.remote_is_newer("2.9.9") is False
    assert api.remote_is_newer(None) is False
    assert api.remote_is_newer("") is False


# ---------------- credential shape validation ----------------
def test_valid_sk():
    assert credentials._valid_sk("sk-ant-sid01-" + "A" * 30) is True
    assert credentials._valid_sk("sk-ant-sid42-" + "aZ0_-" * 6) is True
    assert credentials._valid_sk("sk-ant-sid1-" + "A" * 30) is False  # only 1 digit
    assert credentials._valid_sk("sk-ant-sid01-short") is False        # < 20 tail chars
    assert credentials._valid_sk("not-a-key") is False
    assert credentials._valid_sk("") is False
    assert credentials._valid_sk(None) is False


def test_valid_org():
    assert credentials._valid_org("a443c5ae-2b4e-479f-a12d-e611203db3e7") is True  # v4 uuid
    assert credentials._valid_org("00000000-0000-4000-8000-000000000000") is True
    assert credentials._valid_org("a443c5ae-2b4e-179f-a12d-e611203db3e7") is False  # not v4
    assert credentials._valid_org("a443c5ae-2b4e-479f-c12d-e611203db3e7") is False  # bad variant
    assert credentials._valid_org("nope") is False
    assert credentials._valid_org("") is False
    assert credentials._valid_org(None) is False


# ---------------- ISO datetime parsing ----------------
def test_parse_iso():
    assert model._parse_iso(None) is None
    assert model._parse_iso("") is None
    assert model._parse_iso("garbage") is None
    dt = model._parse_iso("2025-01-02T03:04:05Z")  # 'Z' must be accepted on py<3.11
    assert dt is not None and dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2025, 1, 2, 3)
    naive = model._parse_iso("2025-01-02T03:04:05")  # no tz -> assumed UTC
    assert naive is not None and naive.tzinfo is not None


# ---------------- formatters ----------------
def test_bar():
    assert model._bar(None) == "▕" + "░" * 24 + "▏"
    assert model._bar(0) == "▕" + "░" * 24 + "▏"
    assert model._bar(100) == "▕" + "█" * 24 + "▏"
    assert model._bar(50) == "▕" + "█" * 12 + "░" * 12 + "▏"
    assert model._bar(50, n=10) == "▕" + "█" * 5 + "░" * 5 + "▏"


def test_pct():
    assert model._pct(None) == "--"
    assert model._pct(0) == "0%"
    assert model._pct(50) == "50%"
    assert model._pct(49.6) == "50%"   # rounds
    assert model._pct(99.4) == "99%"


def test_fmt_countdown_edges():
    from datetime import datetime, timedelta, timezone
    assert model._fmt_countdown(None) == "--"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert model._fmt_countdown(past) == "0m"
    future = datetime.now(timezone.utc) + timedelta(hours=2, minutes=4)
    s = model._fmt_countdown(future)
    assert s.endswith("m") and "h" in s


def test_fmt_resetday_edges():
    from datetime import datetime, timezone
    assert model._fmt_resetday(None) == "--"
    s = model._fmt_resetday(datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc))  # a Monday
    assert isinstance(s, str) and len(s) >= 3  # weekday + time, local-tz dependent


# ---------------- json extraction ----------------
def test_json_to_raw():
    j = {
        "five_hour": {"utilization": 39, "resets_at": "2025-01-02T03:04:05Z"},
        "seven_day": {"utilization": 5, "resets_at": "2025-01-06T07:00:00Z"},
        "seven_day_sonnet": {"utilization": 12},
        "seven_day_opus": {"utilization": 0},
    }
    raw = model.json_to_raw(j)
    assert raw["five_hour_util"] == 39
    assert raw["seven_day_util"] == 5
    assert raw["sonnet_util"] == 12
    assert raw["opus_util"] == 0
    assert raw["five_hour_reset"] is not None
    assert raw["opus_reset"] is None  # no resets_at given


def test_json_to_raw_missing():
    raw = model.json_to_raw({})
    assert raw["five_hour_util"] is None
    assert raw["five_hour_reset"] is None


# ---------------- schema validation ----------------
@pytest.fixture
def no_dump(monkeypatch):
    monkeypatch.setattr(api, "dump_diagnostics", lambda *a, **k: "")


def test_validate_ok(no_dump):
    data = {"five_hour": {"utilization": 50}, "seven_day": {"utilization": 10}}
    raw = api.validate_and_extract(data)
    assert raw["five_hour_util"] == 50 and raw["seven_day_util"] == 10


def test_validate_not_object(no_dump):
    with pytest.raises(api.SchemaError):
        api.validate_and_extract(["nope"])


def test_validate_missing_field(no_dump):
    with pytest.raises(api.SchemaError):
        api.validate_and_extract({"five_hour": {"utilization": 1}})  # seven_day missing


def test_validate_bad_type(no_dump):
    with pytest.raises(api.SchemaError):
        api.validate_and_extract({"five_hour": {"utilization": "x"}, "seven_day": {"utilization": 1}})


# ---------------- data model ----------------
def test_usagedata_properties():
    d = model.UsageData(five_hour_util=39, seven_day_util=5, sonnet_util=12, opus_util=0)
    assert d.current_session_used == "39%"
    assert d.all_models_used == "5%"
    assert d.sonnet_used == "12%"
    assert d.opus_used == "0%"


def test_short_label_waiting():
    d = model.UsageData()  # no received_at, status init
    assert "waiting" in d.short_label().lower()


def test_short_label_error_before_first_success():
    d = model.UsageData(status="auth")
    assert "⚠" in d.short_label()


def test_short_label_ok():
    from datetime import datetime
    d = model.UsageData(status="ok", five_hour_util=39, seven_day_util=5, received_at=datetime.now())
    label = d.short_label()
    assert label.startswith("Cur ") and "All" in label and "⚠" not in label


# ---------------- store change-detection ----------------
def _fields(fh=50, sd=10):
    return dict(five_hour_util=fh, five_hour_reset=None, seven_day_util=sd, seven_day_reset=None,
                sonnet_util=None, sonnet_reset=None, opus_util=None, opus_reset=None)


def test_store_first_apply_not_changed():
    store = model.UsageStore()
    # first successful apply: received_at was None, so it is NOT counted as "changed"
    assert store.apply("ok", "", _fields(50, 10)) is False
    assert store.get().five_hour_util == 50


def test_store_same_values_not_changed():
    store = model.UsageStore()
    store.apply("ok", "", _fields(50, 10))
    assert store.apply("ok", "", _fields(50, 10)) is False


def test_store_changed_values():
    store = model.UsageStore()
    store.apply("ok", "", _fields(50, 10))
    assert store.apply("ok", "", _fields(60, 10)) is True


def test_store_failure_increments_and_keeps_data():
    store = model.UsageStore()
    store.apply("ok", "", _fields(50, 10))
    assert store.apply("auth", "boom", None) is False
    d = store.get()
    assert d.status == "auth" and d.error_msg == "boom"
    assert d.consecutive_failures == 1
    assert d.five_hour_util == 50  # data preserved across a failed poll
