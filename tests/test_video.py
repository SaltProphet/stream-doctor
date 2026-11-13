from stream_doctor.video import FrameAnalyzer, is_unexpected_freeze


def test_black_frame_is_detected_without_saving_frame():
    analyzer = FrameAnalyzer(brightness_threshold=8, black_ratio=0.98)
    result = analyzer.observe([[0, 0, 0], [1, 1, 1], [0, 0, 0]])
    assert result.is_black is True
    assert result.is_frozen is False


def test_frozen_frame_requires_repeated_identical_measurements():
    analyzer = FrameAnalyzer(freeze_samples=3)
    analyzer.observe([[100, 100], [100, 100]])
    analyzer.observe([[100, 100], [100, 100]])
    result = analyzer.observe([[100, 100], [100, 100]])
    assert result.is_frozen is True


def test_normal_frame_is_not_black():
    result = FrameAnalyzer().observe([[10, 40, 90], [80, 80, 80]])
    assert result.is_black is False


def test_intentionally_static_source_is_not_reported_as_unexpected_freeze():
    analyzer = FrameAnalyzer(freeze_samples=2)
    analyzer.observe([[50, 50], [50, 50]])
    observation = analyzer.observe([[50, 50], [50, 50]])
    assert observation.is_frozen is True
    assert is_unexpected_freeze(observation, "Overlay", {"Overlay"}) is False
    assert is_unexpected_freeze(observation, "Camera", set()) is True
