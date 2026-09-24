import pytest
from regen_inventory import parse_net0_ip, build_inventory, render_yaml, validate_result


def test_parse_net0_ip_static():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,ip=10.0.0.5/24,type=veth") == "10.0.0.5"


def test_parse_net0_ip_dhcp():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,ip=dhcp,type=veth") is None


def test_parse_net0_ip_missing():
    assert parse_net0_ip("name=eth0,bridge=vmbr0,type=veth") is None


def test_parse_net0_ip_empty():
    assert parse_net0_ip("") is None
    assert parse_net0_ip(None) is None


def test_build_inventory_basic():
    raw = [
        {"name": "a", "vmid": 100, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "b", "vmid": 101, "node": "node1", "net0": "ip=10.0.0.2/24"},
    ]
    hosts, skipped, warnings = build_inventory(raw)
    assert hosts == {
        "a": {"ansible_host": "10.0.0.1", "vmid": 100, "node": "node1"},
        "b": {"ansible_host": "10.0.0.2", "vmid": 101, "node": "node1"},
    }
    assert skipped == []
    assert warnings == []


def test_build_inventory_skips_dhcp_container():
    raw = [
        {"name": "a", "vmid": 100, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "b", "vmid": 101, "node": "node1", "net0": "ip=dhcp"},
    ]
    hosts, skipped, warnings = build_inventory(raw)
    assert "b" not in hosts
    assert skipped == ["b"]
    assert len(warnings) == 1


def test_build_inventory_duplicate_vmid_is_fatal():
    raw = [
        {"name": "claude", "vmid": 127, "node": "node1", "net0": "ip=10.0.0.1/24"},
        {"name": "postgres", "vmid": 127, "node": "node1", "net0": "ip=10.0.0.2/24"},
    ]
    with pytest.raises(ValueError, match="duplicate vmid"):
        build_inventory(raw)


def test_render_yaml_is_sorted_and_valid():
    import yaml

    hosts = {
        "z": {"ansible_host": "10.0.0.9", "vmid": 1, "node": "node1"},
        "a": {"ansible_host": "10.0.0.1", "vmid": 2, "node": "node1"},
    }
    text = render_yaml(hosts)
    assert text.index("a:") < text.index("z:")
    assert yaml.safe_load(text) == {"containers": {"hosts": hosts}}


def test_validate_result_rejects_zero_after_nonzero():
    with pytest.raises(ValueError, match="refusing to overwrite"):
        validate_result(0, 14)


def test_validate_result_rejects_big_drop():
    with pytest.raises(ValueError, match="refusing to overwrite"):
        validate_result(3, 14)


def test_validate_result_allows_small_drop():
    validate_result(13, 14)


def test_validate_result_allows_growth():
    validate_result(20, 14)


def test_validate_result_allows_first_run_with_no_history():
    validate_result(0, 0)


def test_last_known_good_count_missing_cache(monkeypatch, tmp_path):
    import regen_inventory

    monkeypatch.setattr(regen_inventory, "CACHE_PATH", tmp_path / "missing.yml")
    assert regen_inventory.last_known_good_count() == 0


def test_last_known_good_count_reads_cache(monkeypatch, tmp_path):
    import regen_inventory

    cache = tmp_path / "cache.yml"
    cache.write_text(render_yaml({"a": {"ansible_host": "10.0.0.1", "vmid": 1, "node": "node1"}}))
    monkeypatch.setattr(regen_inventory, "CACHE_PATH", cache)
    assert regen_inventory.last_known_good_count() == 1
