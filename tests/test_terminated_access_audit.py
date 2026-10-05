from sailpoint_mcp.tools.terminated_access_audit import assess, build_query, flatten


def _doc(accounts, access=()):
    return {
        "id": "1",
        "displayName": "Sandra.Lopez",
        "attributes": {"jobTitle": "Engineer", "cloudLifecycleState": "inactive"},
        "accounts": accounts,
        "access": list(access),
    }


AD = {
    "name": "Sandra.Lopez",
    "source": {"name": "Active Directory"},
    "disabled": False,
    "entitlementAttributes": {"memberOf": ["a", "b", "c"]},
}


def test_query_covers_state_and_inactive_flag():
    assert build_query("inactive") == '(attributes.cloudLifecycleState:"inactive" OR inactive:true)'


def test_all_accounts_disabled_means_no_finding():
    assert assess(_doc([{**AD, "disabled": True}])) is None


def test_enabled_account_with_groups_is_medium():
    finding = assess(_doc([AD]))
    assert finding["risk"] == "Medium"
    assert finding["live_accounts"][0]["entitlements"] == 3


def test_privileged_access_is_high_and_disabled_accounts_are_ignored():
    finding = assess(
        _doc([AD, {**AD, "name": "x", "disabled": True}], access=[{"displayName": "Domain Admins", "privileged": True}])
    )
    assert finding["risk"] == "High"
    assert len(finding["live_accounts"]) == 1
    assert finding["privileged_access"] == ["Domain Admins"]


def test_flatten_orders_high_risk_first():
    low = assess(_doc([{**AD, "entitlementAttributes": {}}]))
    high = assess(_doc([{**AD, "privileged": True}]))
    assert [r["risk"] for r in flatten([low, high])] == ["High", "Low"]
