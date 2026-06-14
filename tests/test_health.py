import yaml

from app.health import load_config


def test_health_load_config_uses_config_env_var(monkeypatch, tmp_path):
    config_path = tmp_path / "custom-config.yml"
    config_path.write_text("""
frigate:
  frigate_url: http://example.invalid
  mqtt_server: mqtt.example.invalid
  mqtt_port: 1883
""".lstrip())

    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert load_config() == yaml.safe_load(config_path.read_text())
