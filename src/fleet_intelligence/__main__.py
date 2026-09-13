"""Allow ``python -m fleet_intelligence`` as an alternative to ``fleet-ingest``."""

from .main import main

raise SystemExit(main())
