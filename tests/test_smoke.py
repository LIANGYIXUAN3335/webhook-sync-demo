from app import db


def test_healthz_and_seed(client, settings):
    assert client.get("/healthz").json() == {"ok": True}
    with db.session(settings.database_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
    assert count == len(db.SYNTHETIC_ORDERS)
