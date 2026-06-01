from scripts import device_flow


def test_device_flow_uses_identity_only_scope():
    calls = []

    def http_post(url, payload, headers=None):
        calls.append((url, payload, headers))
        return 200, {
            "device_code": "dev",
            "user_code": "USER-CODE",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        }

    device_flow.start_device_flow("client-id", http_post=http_post)

    assert calls[0][1]["scope"] == "read:user user:email"
    assert "repo" not in calls[0][1]["scope"].split()
