"""Unit tests for queries.py"""
from datetime import datetime


# ---------------------------------------------------------------------------
# get_common_name
# ---------------------------------------------------------------------------

def test_get_common_name_found(patched_queries):
    assert patched_queries.get_common_name("Turdus migratorius") == "American Robin"


def test_get_common_name_not_found(patched_queries):
    result = patched_queries.get_common_name("Unknown species")
    assert result is None


# ---------------------------------------------------------------------------
# recent_detections
# ---------------------------------------------------------------------------

def test_recent_detections_returns_list(patched_queries):
    results = patched_queries.recent_detections(5)
    assert isinstance(results, list)


def test_recent_detections_count_respects_limit(patched_queries):
    results = patched_queries.recent_detections(2)
    assert len(results) <= 2


def test_recent_detections_includes_common_name(patched_queries):
    results = patched_queries.recent_detections(5)
    assert len(results) > 0
    for r in results:
        assert "common_name" in r
        assert r["common_name"] != ""


def test_recent_detections_sorted_descending(patched_queries):
    results = patched_queries.recent_detections(5)
    times = [r["detection_time"] for r in results]
    assert times == sorted(times, reverse=True)


# ---------------------------------------------------------------------------
# get_earliest_detection_date
# ---------------------------------------------------------------------------

def test_get_earliest_detection_date_returns_string(patched_queries):
    result = patched_queries.get_earliest_detection_date()
    assert result == "2024-06-01"


# ---------------------------------------------------------------------------
# get_daily_summary
# ---------------------------------------------------------------------------

def test_get_daily_summary_returns_dict(patched_queries):
    dt = datetime(2024, 6, 1)
    summary = patched_queries.get_daily_summary(dt)
    assert isinstance(summary, dict)


def test_get_daily_summary_contains_expected_species(patched_queries):
    dt = datetime(2024, 6, 1)
    summary = patched_queries.get_daily_summary(dt)
    assert "Turdus migratorius" in summary
    assert "Cyanocitta cristata" in summary


def test_get_daily_summary_correct_count(patched_queries):
    dt = datetime(2024, 6, 1)
    summary = patched_queries.get_daily_summary(dt)
    # 2 robin detections, 1 blue jay
    assert summary["Turdus migratorius"]["total_detections"] == 2
    assert summary["Cyanocitta cristata"]["total_detections"] == 1


def test_get_daily_summary_empty_for_no_data(patched_queries):
    dt = datetime(2000, 1, 1)
    summary = patched_queries.get_daily_summary(dt)
    assert summary == {}


# ---------------------------------------------------------------------------
# get_records_for_date_hour
# ---------------------------------------------------------------------------

def test_get_records_for_date_hour_returns_list(patched_queries):
    records = patched_queries.get_records_for_date_hour("2024-06-01", 8)
    assert isinstance(records, list)


def test_get_records_for_date_hour_correct_hour(patched_queries):
    records = patched_queries.get_records_for_date_hour("2024-06-01", 8)
    assert len(records) == 1
    assert records[0]["display_name"] == "Turdus migratorius"


def test_get_records_for_date_hour_empty_hour(patched_queries):
    records = patched_queries.get_records_for_date_hour("2024-06-01", 23)
    assert records == []


# ---------------------------------------------------------------------------
# get_records_for_scientific_name_and_date
# ---------------------------------------------------------------------------

def test_get_records_for_scientific_name_and_date(patched_queries):
    records = patched_queries.get_records_for_scientific_name_and_date(
        "Turdus migratorius", "2024-06-01", 1, 25
    )
    assert len(records) == 2
    for r in records:
        assert r["display_name"] == "Turdus migratorius"
        assert "common_name" in r


def test_get_records_for_scientific_name_and_date_no_match(patched_queries):
    records = patched_queries.get_records_for_scientific_name_and_date(
        "Turdus migratorius", "1999-01-01", 1, 25
    )
    assert records == []
