from types import SimpleNamespace

import src.main as main_module


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_run_records_duration_after_database_work(monkeypatch):
    connection = _Connection()
    captured = {}
    perf_values = iter([0.0, 1.0, 9.0])
    settings = SimpleNamespace(
        gbfs_url="https://example.test/feed.json",
        bbox=None,
        feed_stale_minutes=None,
        volume_drop_threshold=0.5,
        volume_drop_policy="WARNING",
        source_feed="test_feed",
        grid_size_degrees=0.01,
        detail_retention_days=3,
        rejected_retention_days=3,
        rebalance_high_threshold=10,
        rebalance_medium_threshold=5,
    )
    validation = SimpleNamespace(
        source_timestamp=None,
        records_received=1,
        accepted_records=[{"vehicle_id": "a"}],
        rejected_records=[],
        observed_schema_keys=["bike_id"],
    )

    monkeypatch.setattr(main_module.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "connect", lambda _settings: connection)
    monkeypatch.setattr(main_module, "get_recent_baseline_count", lambda _conn: None)
    monkeypatch.setattr(
        main_module,
        "fetch_gbfs",
        lambda _url: SimpleNamespace(payload={"data": {}}, http_status=200, latency_ms=25),
    )
    monkeypatch.setattr(main_module, "validate_payload", lambda _payload, bbox: validation)
    monkeypatch.setattr(main_module, "calculate_quality_status", lambda **kwargs: ("SUCCESS", []))
    monkeypatch.setattr(main_module, "transform_records", lambda *args, **kwargs: [{"vehicle_id": "a"}])

    def capture_load(*args, **kwargs):
        captured["load"] = {**kwargs, "pipeline_run": dict(kwargs["pipeline_run"])}

    monkeypatch.setattr(main_module, "load_snapshot", capture_load)
    monkeypatch.setattr(main_module, "upsert_pipeline_run", lambda conn, run: captured.setdefault("final", dict(run)))

    result = main_module.run()

    assert result == 0
    assert captured["load"]["pipeline_run"]["pipeline_duration_ms"] == 1000
    assert captured["final"]["pipeline_duration_ms"] == 9000
    assert captured["load"]["detail_retention_days"] == 3
    assert captured["load"]["rebalance_high_threshold"] == 10
    assert connection.closed is True
