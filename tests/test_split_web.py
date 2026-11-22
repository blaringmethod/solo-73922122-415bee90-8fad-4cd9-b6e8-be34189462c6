"""HTTP-level split flow on the check page."""

BATCHES = "B01 r2\nB02 r2\nB03 r1\nB04 r1\n"
SHEETS = (
    "S1 150\nB01 r2\nB02 r2\n\n"
    "S2 90\nB03 r1\nB04 r1\n"
)


def do_import(client):
    return client.post(
        "/import", data={"batches": BATCHES, "sheets": SHEETS},
        follow_redirects=True,
    )


def test_split_via_web_then_full_selection_still_reports(client):
    do_import(client)
    response = client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B01A", "child_b": "B01B"},
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "已提交拆批" in text
    assert "B01A" in text and "B01B" in text
    # Side-by-side: original code on the sheet and its current coverage.
    assert "B01" in text and "B01A" in text and "B01B" in text

    query = (
        "/?check=1&batch=B01A&batch=B01B&batch=B02&batch=B03&batch=B04"
    )
    text = client.get(query).get_data(as_text=True)
    assert 'data-status="ok"' in text
    assert "240" in text


def test_one_child_selection_cannot_report(client):
    do_import(client)
    client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B01A", "child_b": "B01B"},
    )
    text = client.get("/?check=1&batch=B01A").get_data(as_text=True)
    assert 'data-status="none"' in text
    assert "不能单独给它报数" in text


def test_split_form_rejects_reused_and_equal_codes(client):
    do_import(client)
    resp = client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B02", "child_b": "B09"},
        follow_redirects=True,
    )
    assert "已被使用" in resp.get_data(as_text=True)

    resp = client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "X1", "child_b": "X1"},
        follow_redirects=True,
    )
    assert "必须不同" in resp.get_data(as_text=True)
    # Still on submission 1.
    assert "当前提交 <strong>#1</strong>" in resp.get_data(as_text=True)


def test_split_then_child_revision_invalidates_old_sheet(client):
    do_import(client)
    client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B01A", "child_b": "B01B"},
    )
    # Page now carries base_id 2 in the revision form.
    page = client.get("/").get_data(as_text=True)
    assert 'name="base_id" value="2"' in page

    resp = client.post(
        "/revision",
        data={"base_id": "2", "code": "B01A", "revision": "3"},
        follow_redirects=True,
    )
    text = resp.get_data(as_text=True)
    assert "已提交更版" in text
    assert "B01A" in text and "不一致" in text

    text = client.get(
        "/?check=1&batch=B01A&batch=B01B&batch=B02"
    ).get_data(as_text=True)
    assert 'data-status="none"' in text


def test_split_rejects_stale_base_id(client):
    do_import(client)
    client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B01A", "child_b": "B01B"},
    )
    text = client.post(
        "/split",
        data={"base_id": "1", "code": "B02", "child_a": "X1", "child_b": "X2"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已不是最新提交" in text


def test_split_then_reimport_reuses_code_cleanly(client):
    do_import(client)
    client.post(
        "/split",
        data={"base_id": "1", "code": "B01", "child_a": "B01A", "child_b": "B01B"},
    )
    # Full re-import reusing B01 and even B01A; lineage starts fresh.
    resp = client.post(
        "/import",
        data={
            "batches": "B01 r1\nB01A r1\n",
            "sheets": "T1 12\nB01 r1\nB01A r1\n",
        },
        follow_redirects=True,
    )
    assert "已提交导入" in resp.get_data(as_text=True)
    text = client.get("/?check=1&batch=B01&batch=B01A").get_data(as_text=True)
    assert 'data-status="ok"' in text
    assert "12" in text


def test_split_limit_sixteen_enforced_over_http(client):
    batches = "\n".join(f"B{i:02d} r1" for i in range(15))
    client.post(
        "/import",
        data={"batches": batches, "sheets": "S1 1\nB00 r1\n"},
    )
    # 15 -> 16 batches: accepted.
    ok = client.post(
        "/split",
        data={"base_id": "1", "code": "B00", "child_a": "C01", "child_b": "C02"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已提交拆批" in ok

    # 16 -> 17 batches: rejected, current data stays at submission 2.
    rejected = client.post(
        "/split",
        data={"base_id": "2", "code": "B01", "child_a": "D01", "child_b": "D02"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "超过上限" in rejected
    assert "当前提交 <strong>#2</strong>" in rejected
