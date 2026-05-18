def test_cache_reuses_same_image_content_across_different_paths(tmp_path, monkeypatch):
    from gateway.platforms import napcat_vision_cache as cache_mod

    cache_file = tmp_path / "napcat_vision_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)

    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"same-image"
    first.write_bytes(payload)
    second.write_bytes(payload)

    assert cache_mod.get_cached_analysis(str(first)) is None
    assert cache_mod.cache_image_analysis(str(first), "A meme cat staring blankly.") is True
    assert cache_mod.get_cached_analysis(str(second)) == "A meme cat staring blankly."


def test_cache_miss_for_nonexistent_file(tmp_path, monkeypatch):
    from gateway.platforms import napcat_vision_cache as cache_mod

    cache_file = tmp_path / "napcat_vision_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)

    missing = tmp_path / "missing.png"
    assert cache_mod.get_cached_analysis(str(missing)) is None
    assert cache_mod.cache_image_analysis(str(missing), "unused") is False
