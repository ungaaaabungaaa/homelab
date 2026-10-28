# Feature queue

Delivered 28 October 2026: `verify --backup PATH` runs a full restore drill in disposable temporary storage. It reports verified rows, bytes and checksum, removes temporary files, and preserves both the snapshot and live data. Corruption fails without a success receipt.

Next useful work:
- Run a documented separate-storage-device restore drill with actual target evidence.
- Assess local authentication needs before adding any remote access.

No user's live network, Docker installation or private monitoring data is changed by development checks.
