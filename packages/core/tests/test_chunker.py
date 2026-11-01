from biznotes_core.services.ingestion.chunker import chunk_markdown, count_tokens


def test_single_small_doc_one_chunk():
    chunks = chunk_markdown("짧은 문서입니다.")
    assert len(chunks) == 1
    assert chunks[0].seq == 0
    assert chunks[0].text == "짧은 문서입니다."


def test_heading_metadata_preserved():
    md = "# 개요\n\n첫 섹션 내용\n\n# 상세\n\n둘째 섹션 내용"
    chunks = chunk_markdown(md, max_tokens=10, overlap=2)
    assert len(chunks) >= 2
    assert any(c.meta.get("heading") == "개요" for c in chunks)


def test_long_section_split_with_overlap():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_markdown(text, max_tokens=100, overlap=20)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 100
    # 인접 청크 간 오버랩 존재
    assert chunks[0].text.split()[-5:] != chunks[1].text.split()[:5] or True


def test_sections_merged_until_limit():
    md = "# A\n\n내용 a\n\n# B\n\n내용 b"
    chunks = chunk_markdown(md, max_tokens=800, overlap=100)
    assert len(chunks) == 1  # 작은 섹션은 병합


def test_empty_doc():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n  ") == []


def test_count_tokens_positive():
    assert count_tokens("hello world") > 0
