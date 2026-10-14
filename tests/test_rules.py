from app.rules import check_sensors


def sev(items, key):
    return [i["severity"] for i in items if i["source"] == f"sensor:{key}"]


def test_critical_temperature():
    items = check_sensors("pump", {"temp_c": 100.0})
    assert "critical" in sev(items, "temp_c")


def test_warning_temperature():
    items = check_sensors("pump", {"temp_c": 90.0})
    assert "warning" in sev(items, "temp_c")


def test_normal_temperature():
    items = check_sensors("pump", {"temp_c": 70.0})
    assert sev(items, "temp_c") == ["ok"]


def test_low_pressure_is_warning():
    items = check_sensors("pump", {"pressure_bar": 2.0})
    assert "warning" in sev(items, "pressure_bar")


def test_negative_current_deviation_uses_absolute_value():
    items = check_sensors("motor", {"current_dev_pct": -30.0})
    assert "critical" in sev(items, "current_dev_pct")


def test_missing_reading_is_flagged():
    items = check_sensors("pump", {"temp_c": 70.0})
    assert "data_quality" in sev(items, "vibration_mm_s")


def test_invalid_reading_is_flagged_not_checked():
    items = check_sensors("pump", {"temp_c": "abc"})
    assert sev(items, "temp_c") == ["data_quality"]


def test_implausible_reading_is_flagged():
    items = check_sensors("pump", {"temp_c": 9999.0})
    assert sev(items, "temp_c") == ["data_quality"]


def test_conflict_description_vs_sensor():
    items = check_sensors("pump", {"temp_c": 60.0}, "The pump is overheating")
    assert any(i["severity"] == "data_quality" and "Conflict" in i["text"] for i in items)


def test_no_conflict_when_sensor_confirms():
    items = check_sensors("pump", {"temp_c": 100.0}, "The pump is overheating")
    assert not any("Conflict" in i["text"] for i in items)


def test_no_sensors_gives_info():
    items = check_sensors("pump", None)
    assert items[0]["severity"] == "info"


def test_unknown_equipment_type():
    items = check_sensors("rocket", {"temp_c": 10.0})
    assert items[0]["severity"] == "data_quality"
