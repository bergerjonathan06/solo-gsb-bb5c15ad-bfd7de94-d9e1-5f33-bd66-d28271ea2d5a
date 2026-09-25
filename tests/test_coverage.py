"""仪器时间窗覆盖查询（GET /api/coverage/{instrument}）接口测试。"""


def _instruments():
    return [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "R", "name": "参考标准器"},
        {"code": "W", "name": "工作仪器"},
    ]


def _certs():
    return [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        # 两张证书在 2026-06-01T00:00:00Z 闭区间交接（端点双重有效）
        {"id": "CW-1", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2026-06-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
        {"id": "CW-2", "instrument_code": "W", "cert_number": "W-2",
         "issued_at": "2026-12-01T00:00:00",
         "valid_from": "2026-06-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]


def _edges():
    return [
        {"child_cert_id": "CW-1", "parent_cert_id": "CR"},
        {"child_cert_id": "CW-2", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]


def _submit(client):
    return client.post(
        "/api/submissions",
        json={"instruments": _instruments(),
              "certificates": _certs(), "edges": _edges()},
    )


COVERAGE = "/api/coverage/W"


def test_coverage_closed_handoff_preserves_instant_conflict(client):
    assert _submit(client).status_code == 201
    r = client.get(
        COVERAGE,
        params=[("start", "2026-01-01T00:00:00Z"),
                ("end", "2027-01-01T00:00:00Z")],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["instrument"] == "W"
    assert data["window_start"].startswith("2026-01-01T00:00:00")
    assert data["window_end"].startswith("2027-01-01T00:00:00")
    assert "2026-06-01T00:00:00Z" in data["grid_points"]

    segs = data["segments"]
    assert data["segment_count"] == len(segs) == 3

    left, point, right = segs
    # 左侧开区间（终点不含交接点），唯一成链
    assert left["resolution"] == "unique" and left["valid"] is True
    assert left["start_inclusive"] is True and left["end_inclusive"] is False
    # 交接点独立成段（零长度、两端都含），瞬时双重有效冲突未被吞掉
    assert point["start"].startswith("2026-06-01T00:00:00")
    assert point["end"].startswith("2026-06-01T00:00:00")
    assert point["start_inclusive"] is True and point["end_inclusive"] is True
    assert point["resolution"] == "ambiguous" and point["valid"] is False
    assert "MULTIPLE_ACTIVE_CERTIFICATES" in point["conflict_codes"]
    assert "MULTIPLE_VALID_CHAINS" in point["conflict_codes"]
    # 右侧开区间（起点不含交接点），恢复唯一成链，末端含窗口终点
    assert right["resolution"] == "unique" and right["valid"] is True
    assert right["start_inclusive"] is False and right["end_inclusive"] is True


def test_coverage_segments_cover_whole_window_and_are_ordered(client):
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-01-01T00:00:00Z"),
                ("end", "2027-01-01T00:00:00Z")],
    )
    segs = r.json()["segments"]
    assert segs[0]["start"].startswith("2026-01-01T00:00:00")
    assert segs[-1]["end"].startswith("2027-01-01T00:00:00")
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["end"] == nxt["start"]
        # 相接边界恰好一含一不含，全窗无重叠无遗漏
        assert prev["end_inclusive"] != nxt["start_inclusive"]


def test_coverage_uniform_window_merges_into_single_closed_segment(client):
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-02-01T00:00:00Z"),
                ("end", "2026-03-01T00:00:00Z")],
    )
    data = r.json()
    assert data["segment_count"] == 1
    seg = data["segments"][0]
    assert seg["resolution"] == "unique" and seg["valid"] is True
    assert seg["start_inclusive"] is True and seg["end_inclusive"] is True
    assert seg["conflict_codes"] == []


def test_coverage_no_active_certificate_after_expiry(client):
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2027-01-02T00:00:00Z"),
                ("end", "2027-02-01T00:00:00Z")],
    )
    segs = r.json()["segments"]
    assert len(segs) == 1
    seg = segs[0]
    assert seg["resolution"] == "broken" and seg["valid"] is False
    assert "NO_ACTIVE_CERTIFICATE" in seg["conflict_codes"]


def test_coverage_expiry_boundary_point_is_included(client):
    _submit(client)
    # 窗口终点恰好是 CW-2 有效期闭端点：该点仍 unique，点后开区间 broken
    r = client.get(
        COVERAGE,
        params=[("start", "2026-12-31T00:00:00Z"),
                ("end", "2027-01-03T00:00:00Z")],
    )
    segs = r.json()["segments"]
    assert len(segs) == 2
    # 末点 2027-01-01T00:00:00Z 仍 unique（闭区间），已并入左侧段 -> 段尾含
    valid_seg, broken_seg = segs
    assert valid_seg["resolution"] == "unique"
    assert valid_seg["end"].startswith("2027-01-01T00:00:00")
    assert valid_seg["start_inclusive"] is True
    assert valid_seg["end_inclusive"] is True
    # 紧随其后的开区间断裂（起点不含），窗口终点含
    assert broken_seg["resolution"] == "broken"
    assert "NO_ACTIVE_CERTIFICATE" in broken_seg["conflict_codes"]
    assert broken_seg["start"].startswith("2027-01-01T00:00:00")
    assert broken_seg["start_inclusive"] is False
    assert broken_seg["end_inclusive"] is True


def test_coverage_edge_validity_boundaries_partition_window(client):
    _submit(client)
    # CW-1 -> CR 的边限定在 [2026-03-01, 2026-05-01]
    r = client.post(
        "/api/edges",
        json={"child_cert_id": "CW-1", "parent_cert_id": "CR",
              "valid_from": "2026-03-01T00:00:00",
              "valid_until": "2026-05-01T00:00:00"},
    )
    assert r.status_code in (200, 201), r.text

    r = client.get(
        COVERAGE,
        params=[("start", "2026-01-01T00:00:00Z"),
                ("end", "2026-05-15T00:00:00Z")],
    )
    segs = r.json()["segments"]
    # broken[左) + unique[03-01 .. 05-01 含端点] + broken(右]
    assert segs[0]["resolution"] == "broken"
    assert segs[0]["end_inclusive"] is False
    mid = segs[1]
    assert mid["resolution"] == "unique"
    assert mid["start"].startswith("2026-03-01T00:00:00")
    assert mid["end"].startswith("2026-05-01T00:00:00")
    assert mid["start_inclusive"] is True and mid["end_inclusive"] is True
    assert segs[2]["resolution"] == "broken"
    assert segs[2]["start_inclusive"] is False


def test_coverage_segment_carries_report_conflicts_and_stats(client):
    # 篡改 CW-1 摘要：unique 段也应带 SIGNATURE_DIGEST_MISMATCH 且 valid=false
    _submit(client)
    client.post(
        "/api/certificates",
        json={
            "id": "CW-1", "instrument_code": "W", "cert_number": "W-1",
            "issued_at": "2026-01-01T00:00:00",
            "valid_from": "2026-01-01T00:00:00",
            "valid_until": "2026-06-01T00:00:00",
            "uncertainty": 0.01, "unit": "mm",
            "signature_digest": "0" * 64,
        },
    )
    r = client.get(
        COVERAGE,
        params=[("start", "2026-02-01T00:00:00Z"),
                ("end", "2026-03-01T00:00:00Z")],
    )
    seg = r.json()["segments"][0]
    assert seg["valid"] is False
    assert "SIGNATURE_DIGEST_MISMATCH" in seg["conflict_codes"]
    assert any(c["code"] == "SIGNATURE_DIGEST_MISMATCH" for c in seg["conflicts"])
    assert seg["error_count"] >= 1
    stats = r.json()["statistics"]
    assert stats["reports_evaluated"] == (
        stats["point_samples"] + stats["interval_samples"]
    )


def test_coverage_rejects_inverted_window(client):
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2027-01-01T00:00:00Z"),
                ("end", "2026-01-01T00:00:00Z")],
    )
    assert r.status_code == 422
    assert "不能晚于" in r.json()["detail"]


def test_coverage_fractional_single_point_after_handoff(client):
    # 交接整秒之后的半秒只属于后一张证书：不得误报双证书冲突
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-06-01T00:00:00.500Z"),
                ("end", "2026-06-01T00:00:00.500Z")],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # 响应保留微秒精度的窗口端点
    assert data["window_start"] == "2026-06-01T00:00:00.500000Z"
    assert data["window_end"] == "2026-06-01T00:00:00.500000Z"
    # 单点窗口只覆盖该时刻：唯一区段为零长度点区段
    assert data["segment_count"] == 1
    seg = data["segments"][0]
    assert seg["start"] == seg["end"] == "2026-06-01T00:00:00.500000Z"
    assert seg["start_inclusive"] is True and seg["end_inclusive"] is True
    assert seg["sample_at"] == "2026-06-01T00:00:00.500000Z"
    assert seg["resolution"] == "unique" and seg["valid"] is True
    assert seg["starts"] == ["CW-2"]
    assert "MULTIPLE_ACTIVE_CERTIFICATES" not in seg["conflict_codes"]


def test_coverage_whole_second_boundary_remains_closed_interval(client):
    # 整秒边界本身仍属于闭区间：交接点瞬时双证书、截止点仍有效
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-06-01T00:00:00Z"),
                ("end", "2026-06-01T00:00:00Z")],
    )
    seg = r.json()["segments"][0]
    assert seg["start"] == seg["end"] == "2026-06-01T00:00:00Z"
    assert seg["resolution"] == "ambiguous" and seg["valid"] is False
    assert seg["starts"] == ["CW-1", "CW-2"]
    assert "MULTIPLE_ACTIVE_CERTIFICATES" in seg["conflict_codes"]

    r = client.get(
        COVERAGE,
        params=[("start", "2027-01-01T00:00:00Z"),
                ("end", "2027-01-01T00:00:00Z")],
    )
    seg = r.json()["segments"][0]
    assert seg["resolution"] == "unique" and seg["valid"] is True
    assert seg["starts"] == ["CW-2"]


def test_coverage_fractional_second_after_expiry_not_falsely_valid(client):
    # 截止整秒之后的任意小数秒位于边界之后：不得误报有效
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2027-01-01T00:00:00.300Z"),
                ("end", "2027-01-01T00:00:00.300Z")],
    )
    assert r.status_code == 200, r.text
    seg = r.json()["segments"][0]
    assert seg["resolution"] == "broken" and seg["valid"] is False
    assert seg["starts"] == []
    assert "NO_ACTIVE_CERTIFICATE" in seg["conflict_codes"]
    # 冲突消息给出真实时刻（内部比较编码不外泄）
    msg = next(c for c in seg["conflicts"]
               if c["code"] == "NO_ACTIVE_CERTIFICATE")
    assert "2027-01-01T00:00:00.300000Z" in msg["message"]


def test_coverage_fractional_window_spanning_boundary(client):
    # 小数秒窗口跨越交接边界：网格按真实时间顺序，瞬时冲突仍独占点区段
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-05-31T23:59:59.500Z"),
                ("end", "2026-06-01T00:00:00.500Z")],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["grid_points"] == [
        "2026-05-31T23:59:59.500000Z",
        "2026-06-01T00:00:00Z",
        "2026-06-01T00:00:00.500000Z",
    ]
    segs = data["segments"]
    assert data["segment_count"] == len(segs) == 3
    left, point, right = segs
    assert left["start"] == "2026-05-31T23:59:59.500000Z"
    assert left["end"] == "2026-06-01T00:00:00Z"
    assert left["start_inclusive"] is True and left["end_inclusive"] is False
    assert left["resolution"] == "unique" and left["starts"] == ["CW-1"]
    assert point["start"] == point["end"] == "2026-06-01T00:00:00Z"
    assert point["start_inclusive"] is True and point["end_inclusive"] is True
    assert point["resolution"] == "ambiguous"
    assert "MULTIPLE_ACTIVE_CERTIFICATES" in point["conflict_codes"]
    assert right["start"] == "2026-06-01T00:00:00Z"
    assert right["end"] == "2026-06-01T00:00:00.500000Z"
    assert right["start_inclusive"] is False and right["end_inclusive"] is True
    assert right["resolution"] == "unique" and right["starts"] == ["CW-2"]
    # 开区间内部采样落在两端点之间的真实中点
    assert right["sample_at"] == "2026-06-01T00:00:00.250000Z"


def test_coverage_fractional_timezone_offset_normalized(client):
    # 带时区偏移的小数秒：换算为 UTC 后保留微秒精度
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-06-01T08:00:00.500+08:00"),
                ("end", "2026-06-01T08:00:00.500+08:00")],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["window_start"] == "2026-06-01T00:00:00.500000Z"
    assert data["window_end"] == "2026-06-01T00:00:00.500000Z"
    assert data["segments"][0]["starts"] == ["CW-2"]


def test_coverage_rejects_fractional_inverted_window(client):
    # 仅小数秒级倒置同样是反向窗口
    _submit(client)
    r = client.get(
        COVERAGE,
        params=[("start", "2026-06-01T00:00:00.500Z"),
                ("end", "2026-06-01T00:00:00Z")],
    )
    assert r.status_code == 422
    assert "不能晚于" in r.json()["detail"]


def test_coverage_requires_start_and_end(client):
    _submit(client)
    r = client.get(COVERAGE)
    assert r.status_code == 422


def test_coverage_missing_instrument_404(client):
    r = client.get(
        "/api/coverage/NOPE",
        params=[("start", "2026-01-01T00:00:00Z"),
                ("end", "2026-02-01T00:00:00Z")],
    )
    assert r.status_code == 404
