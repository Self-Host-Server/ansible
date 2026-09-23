from encrypt_on_receipt import generated_hosts, stale_overrides


def test_generated_hosts_parses_yaml():
    text = (
        "containers:\n  hosts:\n"
        "    a: {ansible_host: 10.0.0.1, vmid: 100, node: node1}\n"
        "    b: {ansible_host: 10.0.0.2, vmid: 101, node: node1}\n"
    )
    assert generated_hosts(text) == {"a", "b"}


def test_generated_hosts_empty():
    assert generated_hosts("") == set()
    assert generated_hosts("containers: {}\n") == set()


def test_stale_overrides_finds_orphan():
    assert stale_overrides({"a", "b"}, {"a", "b", "c"}) == ["c"]


def test_stale_overrides_none_when_matching():
    assert stale_overrides({"a", "b"}, {"a", "b"}) == []


def test_stale_overrides_multiple_sorted():
    assert stale_overrides({"a"}, {"a", "z", "m"}) == ["m", "z"]
