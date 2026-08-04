import json
from pathlib import Path

from x_media_downloader.database import Database
from x_media_downloader.models import (
    Analysis,
    ArticleMetadata,
    Attachment,
    AttachmentRole,
    AttachmentSelection,
    ContentKind,
    CreateJobRequest,
    Job,
    JobStatus,
    MediaType,
    PostMetadata,
)


def sample_analysis() -> Analysis:
    return Analysis(
        id="analysis-1",
        url="https://x.com/i/web/status/42",
        post=PostMetadata(post_id="42", author_name="Creator", author_handle="creator"),
        attachments=[Attachment(id="a-1", index=1, media_type=MediaType.PHOTO, extension="jpg")],
    )


def test_create_job_request_document_defaults_are_backward_compatible() -> None:
    request = CreateJobRequest(analysis_id="analysis-1")

    assert request.selections == []
    assert [output.value for output in request.outputs] == ["media"]
    assert request.include_document_media is True


def test_database_round_trip_and_running_job_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    analysis = sample_analysis()
    database.save_analysis(analysis)
    job = Job(
        id="job-1",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[AttachmentSelection(attachment_id="a-1")],
        destination=str(tmp_path),
        status=JobStatus.RUNNING,
    )
    database.save_job(job)
    database.recover_jobs()
    recovered = database.get_job(job.id)
    assert recovered is not None
    assert recovered.status == JobStatus.QUEUED
    assert recovered.phase == "Recovered after restart"
    assert database.get_analysis(analysis.id) == analysis
    database.close()


def test_database_loads_legacy_analysis_and_job_payloads(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    analysis = sample_analysis()
    job = Job(
        id="legacy-job",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[AttachmentSelection(attachment_id="a-1")],
        destination=str(tmp_path),
    )

    analysis_payload = analysis.model_dump(mode="json")
    analysis_payload.pop("content_kind")
    analysis_payload.pop("article")
    analysis_payload.pop("output_relative_dir")
    analysis_payload["attachments"][0].pop("role")
    analysis_payload["attachments"][0].pop("source_id")
    analysis_payload["attachments"][0].pop("alt_text")

    job_payload = job.model_dump(mode="json")
    for field in (
        "content_kind",
        "article",
        "layout_version",
        "output_dir",
        "outputs",
        "include_document_media",
        "total_steps",
        "completed_steps",
    ):
        job_payload.pop(field)

    with database._connection:
        database._connection.execute(
            "INSERT INTO analyses (id, payload, created_at) VALUES (?, ?, ?)",
            (analysis.id, json.dumps(analysis_payload), analysis.created_at),
        )
        database._connection.execute(
            """INSERT INTO jobs (id, payload, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                job.id,
                json.dumps(job_payload),
                job.status.value,
                job.created_at,
                job.updated_at,
            ),
        )

    restored_analysis = database.get_analysis(analysis.id)
    restored_job = database.get_job(job.id)

    assert restored_analysis is not None
    assert restored_analysis.content_kind.value == "post"
    assert restored_analysis.article is None
    assert restored_analysis.attachments[0].role.value == "post_media"
    assert restored_analysis.attachments[0].source_id is None
    assert restored_analysis.attachments[0].alt_text is None
    assert restored_analysis.output_relative_dir is None
    assert restored_job is not None
    assert restored_job.layout_version == 1
    assert restored_job.output_dir is None
    assert [output.value for output in restored_job.outputs] == ["media"]
    assert restored_job.include_document_media is True
    assert restored_job.total_steps == 0
    assert restored_job.completed_steps == 0
    database.close()


def test_legacy_article_metadata_defaults_to_old_renderer() -> None:
    article = ArticleMetadata.model_validate(
        {
            "id": "article-99",
            "title": "Article title",
            "html": "<p>Article body</p>",
        }
    )

    assert article.html_renderer_version == 0


def test_database_round_trips_article_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "article.db")
    analysis = Analysis(
        id="article-analysis",
        url="https://x.com/i/web/status/99",
        post=PostMetadata(
            post_id="99",
            author_name="Writer",
            author_handle="writer",
            text="Introduction",
        ),
        article=ArticleMetadata(
            id="article-99",
            title="Article title",
            published_at="2026-07-20T10:00:00",
            updated_at="2026-07-21T11:00:00",
            html="<p>Article body</p>",
        ),
        attachments=[
            Attachment(
                id="a-1",
                index=1,
                media_type=MediaType.PHOTO,
                extension="jpg",
                role=AttachmentRole.ARTICLE_COVER,
                alt_text="Article cover",
            )
        ],
        content_kind=ContentKind.ARTICLE,
    )

    database.save_analysis(analysis)

    assert database.get_analysis(analysis.id) == analysis
    database.close()

