import math

import pytest

from fleet_intelligence.config import BoundingBox
from fleet_intelligence.validate import (
    validate_payload,
    validate_records,
    validate_station_information,
    validate_station_status,
    validate_system_information,
)


def vehicle(vehicle_id: str = "a", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "bike_id": vehicle_id,
        "lat": 47.61,
        "lon": -122.33,
        "is_reserved": False,
        "is_disabled": False,
    }
    record.update(overrides)
    return record


def test_v2_and_v3_envelopes_are_supported() -> None:
    v2 = validate_payload({"last_updated": 1_798_632_000, "data": {"bikes": [vehicle()]}})
    v3_record = vehicle()
    v3_record["vehicle_id"] = v3_record.pop("bike_id")
    v3 = validate_payload({"last_updated": "1798632000", "data": {"vehicles": [v3_record]}})
    assert v2.records_received == v3.records_received == 1
    assert v2.source_timestamp == v3.source_timestamp
    assert v3.accepted_records[0].vehicle_id == "a"


def test_vehicle_is_normalized_and_aliases_are_preserved() -> None:
    accepted, rejected, keys = validate_records(
        [vehicle(" a ", vehicle_type="scooter", is_reserved="false", is_disabled=0)]
    )
    assert rejected == []
    assert accepted[0].vehicle_id == "a"
    assert accepted[0].vehicle_type_id == "scooter"
    assert accepted[0].is_reserved is False
    assert "vehicle_type" in keys


def test_normalized_duplicate_ids_are_all_rejected() -> None:
    accepted, rejected, _ = validate_records([vehicle("a"), vehicle(" a ")])
    assert accepted == []
    assert [record.reason_code for record in rejected] == ["DUPLICATE_ID", "DUPLICATE_ID"]


@pytest.mark.parametrize("latitude", [91, math.nan, math.inf, "bad", None])
def test_invalid_latitude_is_rejected(latitude: object) -> None:
    _, rejected, _ = validate_records([vehicle(lat=latitude)])
    assert rejected[0].reason_code == "INVALID_LATITUDE"


@pytest.mark.parametrize("longitude", [-181, math.nan, -math.inf, "bad", None])
def test_invalid_longitude_is_rejected(longitude: object) -> None:
    _, rejected, _ = validate_records([vehicle(lon=longitude)])
    assert rejected[0].reason_code == "INVALID_LONGITUDE"


@pytest.mark.parametrize(
    "record,reason",
    [
        ({"lat": 47.6}, "MISSING_ID"),
        ("bad", "MALFORMED_RECORD"),
        (vehicle(is_reserved=None), "INVALID_STATE"),
        (vehicle(is_disabled="maybe"), "INVALID_STATE"),
    ],
)
def test_bad_records_are_quarantined(record: object, reason: str) -> None:
    accepted, rejected, _ = validate_records([record])
    assert accepted == []
    assert rejected[0].reason_code == reason


def test_out_of_bounds_is_rejected_only_when_configured() -> None:
    bbox = BoundingBox(47.0, 48.0, -123.0, -122.0)
    accepted, rejected, _ = validate_records([vehicle(lat=49)], bbox)
    assert accepted == []
    assert rejected[0].reason_code == "OUT_OF_BOUNDS"


@pytest.mark.parametrize(
    "payload,message",
    [
        ({}, "non-empty"),
        ({"data": []}, "data object"),
        ({"data": {}}, "vehicles or data.bikes"),
    ],
)
def test_invalid_envelope_is_rejected(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_payload(payload)


@pytest.mark.parametrize("timestamp", ["bad", None, 10**30])
def test_invalid_timestamp_becomes_missing(timestamp: object) -> None:
    result = validate_payload({"last_updated": timestamp, "data": {"bikes": [vehicle()]}})
    assert result.source_timestamp is None


def test_system_information_is_normalized() -> None:
    result = validate_system_information(
        {
            "last_updated": 1_798_632_000,
            "data": {
                "system_id": " lime_seattle ",
                "name": "Lime Seattle",
                "language": "en",
                "timezone": "America/Los_Angeles",
                "license_url": "https://example.test/license",
            },
        }
    )
    assert result.record.system_id == "lime_seattle"
    assert result.record.license_url == "https://example.test/license"
    assert "timezone" in result.observed_schema_keys


@pytest.mark.parametrize("missing", ["system_id", "name", "language", "timezone"])
def test_system_information_requires_identity_fields(missing: str) -> None:
    data = {
        "system_id": "lime_seattle",
        "name": "Lime Seattle",
        "language": "en",
        "timezone": "America/Los_Angeles",
    }
    del data[missing]
    with pytest.raises(ValueError, match=missing):
        validate_system_information({"data": data})


def test_station_information_normalizes_and_quarantines() -> None:
    result = validate_station_information(
        {
            "data": {
                "stations": [
                    {
                        "station_id": "seattle",
                        "name": "Seattle",
                        "lat": 47.61,
                        "lon": -122.33,
                        "capacity": 50,
                    },
                    {"station_id": "bad", "name": "Bad", "lat": 999, "lon": 0},
                ]
            }
        }
    )
    assert result.records_received == 2
    assert result.accepted_records[0].capacity == 50
    assert result.rejected_records[0].feed_name == "station_information"
    assert result.rejected_records[0].reason_code == "INVALID_LATITUDE"


def test_station_information_rejects_duplicates_and_bounds() -> None:
    station = {"station_id": "same", "name": "Seattle", "lat": 47.61, "lon": -122.33}
    duplicate = validate_station_information({"data": {"stations": [station, station]}})
    assert len(duplicate.rejected_records) == 2
    bbox = BoundingBox(0, 1, 0, 1)
    outside = validate_station_information({"data": {"stations": [station]}}, bbox)
    assert outside.rejected_records[0].reason_code == "OUT_OF_BOUNDS"


def test_station_status_normalizes_v1_alias_and_timestamp() -> None:
    result = validate_station_status(
        {
            "data": {
                "stations": [
                    {
                        "station_id": "seattle",
                        "num_bikes_available": 10,
                        "num_docks_available": 20,
                        "is_installed": 1,
                        "is_renting": "true",
                        "is_returning": True,
                        "last_reported": 1_798_632_000,
                    }
                ]
            }
        }
    )
    status = result.accepted_records[0]
    assert status.num_vehicles_available == 10
    assert status.is_renting is True
    assert status.last_reported is not None


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"num_vehicles_available": -1}, "INVALID_COUNTS"),
        ({"is_renting": "unknown"}, "INVALID_STATE"),
        ({"last_reported": "yesterday"}, "INVALID_TIMESTAMP"),
    ],
)
def test_bad_station_status_is_quarantined(override: dict[str, object], reason: str) -> None:
    station: dict[str, object] = {
        "station_id": "seattle",
        "num_vehicles_available": 10,
        "num_docks_available": 20,
        "is_installed": True,
        "is_renting": True,
        "is_returning": True,
    }
    station.update(override)
    result = validate_station_status({"data": {"stations": [station]}})
    assert result.rejected_records[0].reason_code == reason
    assert result.rejected_records[0].feed_name == "station_status"
