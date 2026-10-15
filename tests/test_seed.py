from scripts.seed import DEMO_REPORTS, seed
from tests.test_reports import make_client


def test_seed_creates_all_demo_equipment():
    client = make_client()
    assert seed(client) == len(DEMO_REPORTS)
    page = client.get("/equipment").text
    for data in DEMO_REPORTS:
        assert data["identifier"] in page


def test_demo_data_covers_conflict_missing_and_critical_cases():
    client = make_client()
    seed(client)
    texts = []
    for i in range(1, len(DEMO_REPORTS) + 1):
        texts += [f["text"] for f in client.get(f"/api/reports/{i}").json()["findings"]]
    joined = " ".join(texts)
    assert "Conflict" in joined
    assert "reading is missing" in joined
    assert "critical limit" in joined
    assert "No sensor readings provided" in joined
