from shared.agent_dispatch import build_dispatch_metadata


def test_build_dispatch_metadata_with_instructions() -> None:
    metadata = build_dispatch_metadata(agent="assistant", instructions="Be concise")
    assert metadata["agent"] == "assistant"
    assert metadata["room_mode"] == "human_ai"
    assert metadata["instructions"] == "Be concise"


def test_build_dispatch_metadata_without_instructions() -> None:
    metadata = build_dispatch_metadata(agent="support", instructions=None)
    assert metadata == {"agent": "support", "room_mode": "human_ai"}
