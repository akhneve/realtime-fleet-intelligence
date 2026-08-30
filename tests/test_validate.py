from src.config import BoundingBox
from src.validate import validate_payload, validate_records


def test_valid_coordinates_accepted():
    accepted, rejected, _ = validate_records(
        [{"bike_id": "a", "lat": 47.61, "lon": -122.33, "is_reserved": False, "is_disabled": False}]
    )

    assert len(accepted) == 1
    assert rejected == []
    assert accepted[0]["vehicle_id"] == "a"


def test_latitude_over_90_rejected():
    _, rejected, _ = validate_records([{"bike_id": "a", "lat": 91, "lon": -122.33}])

    assert rejected[0].reason_code == "INVALID_LATITUDE"


def test_longitude_under_minus_180_rejected():
    _, rejected, _ = validate_records([{"bike_id": "a", "lat": 47.61, "lon": -181}])

    assert rejected[0].reason_code == "INVALID_LONGITUDE"


def test_missing_vehicle_id_rejected():
    _, rejected, _ = validate_records([{"lat": 47.61, "lon": -122.33}])

    assert rejected[0].reason_code == "MISSING_ID"


def test_duplicate_vehicle_ids_are_all_rejected():
    accepted, rejected, _ = validate_records(
        [
            {"bike_id": "a", "lat": 47.61, "lon": -122.33},
            {"bike_id": "a", "lat": 47.62, "lon": -122.34},
        ]
    )

    assert accepted == []
    assert [row.reason_code for row in rejected] == ["DUPLICATE_ID", "DUPLICATE_ID"]


def test_malformed_record_quarantined():
    _, rejected, _ = validate_records(["bad"])

    assert rejected[0].reason_code == "MALFORMED_RECORD"


def test_out_of_bounds_rejected_when_bbox_configured():
    bbox = BoundingBox(min_lat=47.0, max_lat=48.0, min_lon=-123.0, max_lon=-122.0)
    _, rejected, _ = validate_records(
        [{"bike_id": "a", "lat": 49, "lon": -122.33, "is_reserved": False, "is_disabled": False}], bbox=bbox
    )

    assert rejected[0].reason_code == "OUT_OF_BOUNDS"


def test_feed_level_validation_extracts_bikes_and_timestamp():
    result = validate_payload(
        {
            "last_updated": 1798632000,
            "data": {
                "bikes": [
                    {"bike_id": "a", "lat": 47.61, "lon": -122.33, "is_reserved": False, "is_disabled": False}
                ]
            },
        }
    )

    assert result.records_received == 1
    assert result.source_timestamp is not None


def test_missing_state_flags_are_rejected_instead_of_marked_available():
    accepted, rejected, _ = validate_records([{"bike_id": "a", "lat": 47.61, "lon": -122.33}])

    assert accepted == []
    assert rejected[0].reason_code == "INVALID_STATE"


def test_lime_vehicle_type_alias_is_preserved():
    accepted, rejected, _ = validate_records(
        [
            {
                "bike_id": "a",
                "lat": 47.61,
                "lon": -122.33,
                "is_reserved": False,
                "is_disabled": False,
                "vehicle_type": "scooter",
            }
        ]
    )

    assert rejected == []
    assert accepted[0]["vehicle_type_id"] == "scooter"
