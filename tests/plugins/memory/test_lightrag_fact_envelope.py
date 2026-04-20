from plugins.memory.lightrag.fact_envelope import (
    FactEnvelope,
    build_fact_file_source,
    extract_fact_content,
    parse_fact_file_source,
    render_fact_envelope,
)
from plugins.memory.lightrag.query_lanes import classify_query


def test_build_fact_file_source():
    path = build_fact_file_source(
        FactEnvelope(
            fact_type="user_profile",
            subject_scope="napcat_user_46895556",
            source_scope="napcat_user_46895556",
            source_kind="dm",
            target="user",
            content="46895556 的名字是 SIGTERM。",
            subject_user_id="46895556",
            source_chat_id="46895556",
        )
    )
    assert path.startswith("hermes_facts/v1/platform=napcat/fact_type=user_profile/")
    assert "subject=napcat_user_46895556" in path
    assert "source=napcat_user_46895556" in path
    assert "source_kind=dm" in path
    assert "target=user" in path


def test_parse_fact_file_source():
    metadata = parse_fact_file_source(
        "hermes_facts/v1/platform=napcat/fact_type=user_profile/"
        "subject=napcat_user_46895556/source=napcat_user_46895556/"
        "source_kind=dm/target=user/digest=abc.md"
    )
    assert metadata["fact_type"] == "user_profile"
    assert metadata["subject"] == "napcat_user_46895556"
    assert metadata["source"] == "napcat_user_46895556"


def test_render_fact_envelope_contains_human_source_label():
    text = render_fact_envelope(
        FactEnvelope(
            fact_type="user_profile",
            subject_scope="napcat_user_46895556",
            source_scope="napcat_user_46895556",
            source_kind="dm",
            target="user",
            content="46895556 的名字是 SIGTERM。",
            subject_user_id="46895556",
            source_chat_id="46895556",
        )
    )
    assert "信源：与 46895556 的私聊" in text
    assert extract_fact_content(text) == "46895556 的名字是 SIGTERM。"


def test_classify_identity_query():
    assert classify_query("我是谁").lane == "identity"


def test_classify_shared_query():
    assert classify_query("真和平项目之前怎么说").lane == "shared_context"


def test_classify_mixed_query():
    assert classify_query("我在这个项目里负责什么").lane == "mixed"
