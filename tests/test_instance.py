from pathlib import Path

from x_media_downloader.instance import InstanceLock


def test_instance_lock_publishes_url_and_rejects_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)
    assert first.acquire() is True
    first.publish("http://127.0.0.1:8765")
    assert second.acquire() is False
    assert second.current_url() == "http://127.0.0.1:8765"
    first.close()
    assert second.acquire() is True
    second.close()

