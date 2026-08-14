from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from x_media_downloader import extractors
from x_media_downloader import thread as thread_module
from x_media_downloader.documents import render_thread_markdown, render_thread_pdf
from x_media_downloader.layout import build_thread_export_layout, thread_media_name
from x_media_downloader.models import (
    Analysis,
    Attachment,
    MediaType,
    PostMetadata,
    Scope,
)
from x_media_downloader.thread import build_thread

PNG_BUFFER = BytesIO()
Image.new("RGB", (24, 16), color="navy").save(PNG_BUFFER, format="PNG")
PNG_BYTES = PNG_BUFFER.getvalue()


def _post(post_id: str, handle: str = "claude", **overrides: object) -> PostMetadata:
    fields = dict(
        post_id=post_id,
        author_name="Claude",
        author_handle=handle,
        text=f"post {post_id}",
        posted_at="2026-06-09T17:00:00+00:00",
    )
    fields.update(overrides)
    return PostMetadata(**fields)


def _analysis(
    post_id: str,
    *,
    reply_to: str | None = None,
    reply_to_author: str | None = None,
    quoted: str | None = None,
    reposted: str | None = None,
    reply_count: int | None = None,
    attachments: list[Attachment] | None = None,
    handle: str = "claude",
) -> Analysis:
    return Analysis(
        id=f"analysis-{post_id}",
        url=f"https://x.com/i/web/status/{post_id}",
        post=_post(
            post_id,
            handle=handle,
            reply_to_post_id=reply_to,
            reply_to_author=reply_to_author,
            quoted_post_id=quoted,
            reposted_post_id=reposted,
            reply_count=reply_count,
        ),
        attachments=attachments or [],
    )


def make_fetch(*analyses: Analysis) -> object:
    table = {item.post.post_id: item for item in analyses}

    async def fetch(url: str) -> Analysis:
        post_id = url.rstrip("/").rsplit("/", 1)[-1]
        if post_id not in table:
            raise extractors.AnalysisError("This post could not be found or has been deleted.")
        return table[post_id]

    return fetch


@pytest.mark.asyncio
async def test_thread_walk_collects_same_author_ancestors_in_chain_order() -> None:
    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")

    thread = await build_thread(focal, fetch=make_fetch(root, middle))

    assert [member.post_id for member in thread.members] == ["111", "222", "333"]
    assert thread.root_post_id == "111"
    assert thread.focal_post_id == "333"
    assert thread.author_handle == "claude"
    assert not [i for i in thread.issues if i.code in {"THREAD_CYCLE", "THREAD_PARENT_UNAVAILABLE"}]


@pytest.mark.asyncio
async def test_thread_from_middle_member_finds_only_same_author_prefix() -> None:
    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")

    thread = await build_thread(middle, fetch=make_fetch(root))

    assert [member.post_id for member in thread.members] == ["111", "222"]
    assert thread.root_post_id == "111"


@pytest.mark.asyncio
async def test_root_post_yields_single_member_thread_with_info_issue() -> None:
    root = _analysis("111")

    thread = await build_thread(root, fetch=make_fetch())

    assert [member.post_id for member in thread.members] == ["111"]
    assert any(issue.code == "THREAD_NO_ANCESTORS" for issue in thread.issues)


@pytest.mark.asyncio
async def test_reply_to_foreign_author_stops_without_fetching() -> None:
    called: list[str] = []

    async def fetch(url: str) -> Analysis:
        called.append(url)
        raise AssertionError("must not fetch a foreign parent")

    focal = _analysis("333", reply_to="999", reply_to_author="someone_else")

    thread = await build_thread(focal, fetch=fetch)

    assert [member.post_id for member in thread.members] == ["333"]
    assert any(issue.code == "THREAD_FOREIGN_PARENT" for issue in thread.issues)
    assert called == []


@pytest.mark.asyncio
async def test_quoted_and_reposted_posts_are_not_thread_members() -> None:
    quoted = _analysis("333", quoted="888", reposted="777")

    thread = await build_thread(quoted, fetch=make_fetch())

    assert [member.post_id for member in thread.members] == ["333"]
    assert thread.members[0].analysis.post.quoted_post_id == "888"
    assert thread.members[0].analysis.post.reposted_post_id == "777"


@pytest.mark.asyncio
async def test_missing_parent_records_unavailable_issue() -> None:
    focal = _analysis("222", reply_to="111", reply_to_author="claude")

    thread = await build_thread(focal, fetch=make_fetch())

    assert [member.post_id for member in thread.members] == ["222"]
    assert any(issue.code == "THREAD_PARENT_UNAVAILABLE" for issue in thread.issues)


@pytest.mark.asyncio
async def test_cycle_parent_reference_stops_without_infinite_loop() -> None:
    loopy = _analysis("111", reply_to="111", reply_to_author="claude")

    thread = await build_thread(loopy, fetch=make_fetch())

    assert [member.post_id for member in thread.members] == ["111"]
    assert any(issue.code == "THREAD_CYCLE" for issue in thread.issues)


@pytest.mark.asyncio
async def test_max_members_cap_keeps_nearest_ancestors_and_records_limit_issue() -> None:
    post_ids = ["111", "222", "333", "444", "555"]
    chain = [_analysis(post_ids[0])]
    chain += [
        _analysis(post_ids[index], reply_to=post_ids[index - 1], reply_to_author="claude")
        for index in range(1, len(post_ids))
    ]
    focal = chain[-1]

    thread = await build_thread(focal, fetch=make_fetch(*chain), max_members=3)

    assert [member.post_id for member in thread.members] == ["333", "444", "555"]
    assert any(issue.code == "THREAD_LIMIT_REACHED" for issue in thread.issues)


@pytest.mark.asyncio
async def test_shuffled_post_ids_still_order_root_to_focal() -> None:
    root = _analysis("300")
    middle = _analysis("200", reply_to="300", reply_to_author="claude")
    focal = _analysis("100", reply_to="200", reply_to_author="claude")

    thread = await build_thread(focal, fetch=make_fetch(root, middle))

    assert [member.post_id for member in thread.members] == ["300", "200", "100"]


@pytest.mark.asyncio
async def test_replies_exist_info_issue_when_focal_has_replies() -> None:
    focal = _analysis("111", reply_count=5)

    thread = await build_thread(focal, fetch=make_fetch())

    assert any(issue.code == "THREAD_REPLIES_EXIST" for issue in thread.issues)


def test_thread_media_names_are_collision_free() -> None:
    first = thread_media_name("111", "01.jpg")
    second = thread_media_name("222", "01.jpg")

    assert first == "111-01.jpg"
    assert first != second


@pytest.mark.asyncio
async def test_thread_layout_uses_root_post_id(tmp_path: Path) -> None:
    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, middle))

    layout = build_thread_export_layout(tmp_path, thread)

    assert layout.output_dir == tmp_path / "@claude" / "111"
    assert layout.media_dir == layout.output_dir / "media"
    assert layout.markdown_path.name == "thread.md"
    assert layout.pdf_path.name == "thread.pdf"


@pytest.mark.asyncio
async def test_render_thread_markdown_includes_header_members_and_issues() -> None:
    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")
    focal = _analysis("333", reply_to="222", reply_to_author="claude", reply_count=3)
    thread = await build_thread(focal, fetch=make_fetch(root, middle))

    markdown = render_thread_markdown(thread, {}).decode()

    assert "# Author thread" in markdown
    assert "@claude" in markdown
    assert "post 111" in markdown
    assert "post 333" in markdown
    assert "THREAD_REPLIES_EXIST" in markdown


@pytest.mark.asyncio
async def test_render_thread_markdown_links_media_per_member(tmp_path: Path) -> None:
    member_media_path = tmp_path / "333-01.jpg"
    member_media_path.write_bytes(PNG_BYTES)
    focal = _analysis(
        "333",
        reply_to="222",
        reply_to_author="claude",
        attachments=[Attachment(id="a-1", index=1, media_type=MediaType.PHOTO, extension="jpg")],
    )
    thread = await build_thread(focal, fetch=make_fetch())

    markdown = render_thread_markdown(thread, {"333": {"a-1": member_media_path}}).decode()

    assert "333-01.jpg" in markdown


@pytest.mark.asyncio
async def test_render_thread_pdf_is_deterministic_and_contains_all_members() -> None:
    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, middle))

    first = render_thread_pdf(thread, {})
    second = render_thread_pdf(thread, {})

    assert first == second
    assert first.startswith(b"%PDF-")
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(first)).pages
    )
    assert "post 111" in text
    assert "post 333" in text
    assert "Author thread" in text


@pytest.mark.asyncio
async def test_database_round_trip_preserves_thread(tmp_path: Path) -> None:
    from x_media_downloader.database import Database

    root = _analysis("111")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root))
    focal.thread = thread
    database = Database(tmp_path / "state.db")
    database.save_analysis(focal)

    loaded = database.get_analysis(focal.id)

    assert loaded is not None
    assert loaded.thread == thread
    database.close()


@pytest.mark.asyncio
async def test_analyze_url_default_scope_keeps_single_post_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = {
        "author": {"name": "claude", "nick": "Claude"},
        "content": "Hello",
        "date": "2026-06-09T17:00:00",
        "reply_id": 0,
        "conversation_id": 0,
    }

    async def fake_gallery(url: str, **_: object) -> tuple[dict, list[object]]:
        return post, []

    async def fake_ytdlp(url: str) -> list[dict]:
        return []

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)

    analysis = await extractors.analyze_url("https://x.com/claude/status/333")

    assert analysis.thread is None
    assert analysis.post.reply_to_post_id is None
    assert analysis.post.author_handle == "claude"


@pytest.mark.asyncio
async def test_analyze_url_thread_scope_attaches_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    post = {
        "author": {"name": "claude", "nick": "Claude"},
        "content": "Hello",
        "date": "2026-06-09T17:00:00",
        "reply_id": 222,
        "reply_to": "claude",
        "conversation_id": 111,
    }

    async def fake_gallery(url: str, **_: object) -> tuple[dict, list[object]]:
        return post, []

    async def fake_ytdlp(url: str) -> list[dict]:
        return []

    monkeypatch.setattr(extractors, "resolve_gallery_media", fake_gallery)
    monkeypatch.setattr(extractors, "resolve_ytdlp_entries", fake_ytdlp)
    built: list[Analysis] = []

    async def fake_build(focal: Analysis, **_kwargs: object) -> object:
        built.append(focal)
        return None

    monkeypatch.setattr(thread_module, "build_thread", fake_build)

    analysis = await extractors.analyze_url(
        "https://x.com/claude/status/333", scope=Scope.THREAD
    )

    assert built == [analysis]
    assert analysis.post.reply_to_post_id == "222"
    assert analysis.post.reply_to_author == "claude"
    assert analysis.post.conversation_id == "111"

@pytest.mark.asyncio
async def test_thread_job_exports_media_and_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from x_media_downloader.database import Database
    from x_media_downloader.models import Job, JobStatus, OutputFormat
    from x_media_downloader.queue import DownloadQueue

    root = _analysis("111")
    middle = _analysis(
        "222",
        reply_to="111",
        reply_to_author="claude",
        attachments=[Attachment(id="a-1", index=1, media_type=MediaType.PHOTO, extension="jpg")],
    )
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, middle))
    focal.thread = thread
    database = Database(tmp_path / "state.db")
    database.save_analysis(focal)
    job = Job(
        id="job-thread",
        analysis_id=focal.id,
        url=focal.url,
        post=focal.post,
        selections=[],
        outputs=[OutputFormat.MEDIA, OutputFormat.MARKDOWN, OutputFormat.PDF],
        destination=str(tmp_path),
        layout_version=2,
    )
    database.save_job(job)

    async def fake_download_photo(
        self: object,
        job: Job,
        analysis: Analysis,
        attachment: Attachment,
        destination: Path,
        completed_base: int,
        **kwargs: object,
    ) -> tuple[Path, int]:
        del self, job, analysis, destination, completed_base
        target = kwargs.get("target")
        target = Path(target) if target else Path("01.jpg")
        target.write_bytes(PNG_BYTES)
        return target, len(PNG_BYTES)

    monkeypatch.setattr(DownloadQueue, "_download_photo", fake_download_photo)

    queue = DownloadQueue(database)
    await queue._execute(job)

    completed = database.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.progress == 100
    out = tmp_path / "@claude" / "111"
    assert (out / "thread.md").exists()
    assert (out / "thread.pdf").exists()
    assert (out / "media" / "222-01.jpg").exists()
    assert completed.fidelity_issues == []
    assert completed.output_dir == str(out)
    database.close()


@pytest.mark.asyncio
async def test_thread_job_rerun_keeps_existing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from x_media_downloader.database import Database
    from x_media_downloader.models import Job, JobStatus, OutputFormat
    from x_media_downloader.queue import DownloadQueue

    root = _analysis("111")
    middle = _analysis("222", reply_to="111", reply_to_author="claude")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, middle))
    focal.thread = thread
    database = Database(tmp_path / "state.db")
    database.save_analysis(focal)
    job = Job(
        id="job-thread-rerun",
        analysis_id=focal.id,
        url=focal.url,
        post=focal.post,
        selections=[],
        outputs=[OutputFormat.MARKDOWN],
        destination=str(tmp_path),
        layout_version=2,
    )
    database.save_job(job)

    queue = DownloadQueue(database)
    await queue._execute(job)
    first = database.get_job(job.id)
    assert first is not None and first.status == JobStatus.COMPLETED
    out = tmp_path / "@claude" / "111"
    assert (out / "thread.md").exists()

    job.status = JobStatus.QUEUED
    database.save_job(job)
    await queue._execute(job)
    second = database.get_job(job.id)
    assert second is not None and second.status == JobStatus.COMPLETED
    assert "# Author thread" in (out / "thread.md").read_text()
    assert "post 333" in (out / "thread.md").read_text()
    database.close()


@pytest.mark.asyncio
async def test_thread_mixed_article_and_normal_members_render_both() -> None:
    from x_media_downloader.models import ArticleMetadata, ContentKind

    article_member = _analysis("222", reply_to="111", reply_to_author="claude")
    article_member.content_kind = ContentKind.ARTICLE
    article_member.article = ArticleMetadata(
        id="art-222",
        title="Fable notes",
        html="<h2>Details</h2><p>Benchmark lead.</p>",
        html_renderer_version=1,
    )
    root = _analysis("111")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, article_member))

    markdown = render_thread_markdown(thread, {}).decode()
    pdf = render_thread_pdf(thread, {})
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "Fable notes" in markdown
    assert "Benchmark lead." in pdf_text
    assert "post 111" in markdown


@pytest.mark.asyncio
async def test_thread_job_with_article_member_records_no_false_fidelity_errors(
    tmp_path: Path,
) -> None:
    from x_media_downloader.database import Database
    from x_media_downloader.models import (
        ArticleMetadata,
        ContentKind,
        Job,
        JobStatus,
        OutputFormat,
    )
    from x_media_downloader.queue import DownloadQueue

    article_member = _analysis("222", reply_to="111", reply_to_author="claude")
    article_member.content_kind = ContentKind.ARTICLE
    article_member.article = ArticleMetadata(
        id="art-222",
        title="Fable notes",
        html="<h2>Details</h2><p>Benchmark lead.</p>",
        html_renderer_version=1,
    )
    root = _analysis("111")
    focal = _analysis("333", reply_to="222", reply_to_author="claude")
    thread = await build_thread(focal, fetch=make_fetch(root, article_member))
    focal.thread = thread
    database = Database(tmp_path / "state.db")
    database.save_analysis(focal)
    job = Job(
        id="job-thread-article",
        analysis_id=focal.id,
        url=focal.url,
        post=focal.post,
        selections=[],
        outputs=[OutputFormat.MARKDOWN, OutputFormat.PDF],
        destination=str(tmp_path),
        layout_version=2,
    )
    database.save_job(job)

    queue = DownloadQueue(database)
    await queue._execute(job)

    completed = database.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED
    assert completed.fidelity_issues == []
    database.close()
