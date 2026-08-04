from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException

import x_media_downloader.app as app_module
from x_media_downloader.app import (
    _completed_job_is_reusable,
    _selection_key,
    _validate_selections,
    app,
)
from x_media_downloader.models import (
    Analysis,
    Attachment,
    AttachmentSelection,
    Job,
    MediaType,
    OutputFormat,
    PostMetadata,
    QualityOption,
    Settings,
)


@pytest.mark.asyncio
async def test_static_app_and_local_host_guard() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "local X post exporter" in response.text
        rejected = await client.get("/api/health", headers={"host": "remote.example"})
        assert rejected.status_code == 403


async def post_reveal(job_id: str, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(f"/api/jobs/{job_id}/reveal", json=body)


def make_reveal_job(
    root: Path,
    *,
    job_id: str = "job-reveal",
    output_dir: Path | None = None,
    completed_files: list[str] | None = None,
    legacy: bool = False,
) -> Job:
    job_root = output_dir or root / "@creator" / "42"
    return Job(
        id=job_id,
        analysis_id="analysis-1",
        url="https://x.com/i/web/status/42",
        post=PostMetadata(
            post_id="42", author_name="Creator", author_handle="creator"
        ),
        selections=[],
        destination=str(job_root if legacy else root),
        output_dir=None if legacy else str(job_root),
        completed_files=completed_files or [],
    )


def configure_reveal_database(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    job: Job | None,
) -> None:
    monkeypatch.setattr(
        app_module.database,
        "get_job",
        lambda job_id: job if job and job_id == job.id else None,
    )
    monkeypatch.setattr(
        app_module.database,
        "get_settings",
        lambda: Settings(download_dir=str(root)),
    )


def mock_explorer_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Mock, Mock]:
    run = Mock(return_value=SimpleNamespace(stdout=r"C:\Exports\revealed" + "\n"))
    popen = Mock()
    monkeypatch.setattr(app_module.subprocess, "run", run)
    monkeypatch.setattr(app_module.subprocess, "Popen", popen)
    return run, popen


@pytest.mark.asyncio
async def test_reveal_opens_output_folder_and_selects_completed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_root = tmp_path / "@creator" / "42"
    job_root.mkdir(parents=True)
    completed_file = job_root / "post.md"
    completed_file.write_text("post")
    job = make_reveal_job(tmp_path, completed_files=[str(completed_file)])
    configure_reveal_database(monkeypatch, tmp_path, job)
    run, popen = mock_explorer_processes(monkeypatch)

    folder_response = await post_reveal(job.id, {"target": "output_folder"})
    file_response = await post_reveal(
        job.id,
        {"target": "completed_file", "completed_file_index": 0},
    )

    assert folder_response.status_code == 200
    assert folder_response.json() == {"ok": True}
    assert file_response.status_code == 200
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["wslpath", "-w", str(job_root)]
    assert run.call_args_list[0].kwargs == {
        "check": True,
        "capture_output": True,
        "text": True,
        "shell": False,
    }
    assert popen.call_args_list[0].args[0] == ["explorer.exe", r"C:\Exports\revealed"]
    assert popen.call_args_list[1].args[0] == [
        "explorer.exe",
        "/select,",
        r"C:\Exports\revealed",
    ]
    assert all(call.kwargs["shell"] is False for call in popen.call_args_list)


@pytest.mark.asyncio
async def test_reveal_uses_destination_for_legacy_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_root = tmp_path / "legacy"
    job_root.mkdir()
    job = make_reveal_job(tmp_path, output_dir=job_root, legacy=True)
    configure_reveal_database(monkeypatch, tmp_path, job)
    run, _ = mock_explorer_processes(monkeypatch)

    response = await post_reveal(job.id, {"target": "output_folder"})

    assert response.status_code == 200
    assert run.call_args.args[0] == ["wslpath", "-w", str(job_root)]


@pytest.mark.asyncio
async def test_reveal_rejects_arbitrary_path_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_root = tmp_path / "@creator" / "42"
    job_root.mkdir(parents=True)
    job = make_reveal_job(tmp_path)
    configure_reveal_database(monkeypatch, tmp_path, job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(
        job.id,
        {"target": "output_folder", "path": "/tmp/untrusted"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (
            {"target": "completed_file"},
            "completed_file_index is required for a completed file.",
        ),
        (
            {"target": "completed_file", "completed_file_index": 1},
            "completed_file_index does not identify a completed file.",
        ),
        (
            {"target": "output_folder", "completed_file_index": 0},
            "completed_file_index is only valid for a completed file.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reveal_rejects_missing_or_invalid_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
    detail: str,
) -> None:
    job_root = tmp_path / "@creator" / "42"
    job_root.mkdir(parents=True)
    completed_file = job_root / "post.md"
    completed_file.write_text("post")
    job = make_reveal_job(tmp_path, completed_files=[str(completed_file)])
    configure_reveal_database(monkeypatch, tmp_path, job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(job.id, body)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_rejects_negative_index_during_request_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_reveal_database(monkeypatch, tmp_path, None)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(
        "job-reveal",
        {"target": "completed_file", "completed_file_index": -1},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "greater_than_equal"
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_returns_not_found_for_missing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_reveal_database(monkeypatch, tmp_path, None)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal("missing", {"target": "output_folder"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Download job not found."}
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("missing", "body", "detail"),
    [
        (
            "configured_root",
            {"target": "output_folder"},
            "Configured download folder does not exist.",
        ),
        (
            "job_root",
            {"target": "output_folder"},
            "Job output folder does not exist.",
        ),
        (
            "completed_file",
            {"target": "completed_file", "completed_file_index": 0},
            "Completed file does not exist.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reveal_returns_not_found_for_missing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    body: dict,
    detail: str,
) -> None:
    configured_root = tmp_path / "downloads"
    job_root = configured_root / "@creator" / "42"
    completed_file = job_root / "post.md"
    if missing != "configured_root":
        configured_root.mkdir()
    if missing != "job_root" and missing != "configured_root":
        job_root.mkdir(parents=True)
    job = make_reveal_job(
        configured_root,
        output_dir=job_root,
        completed_files=[str(completed_file)],
    )
    configure_reveal_database(monkeypatch, configured_root, job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(job.id, body)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_rejects_job_outside_current_download_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_root = tmp_path / "current"
    configured_root.mkdir()
    job_root = tmp_path / "old" / "@creator" / "42"
    job_root.mkdir(parents=True)
    job = make_reveal_job(configured_root, output_dir=job_root)
    configure_reveal_database(monkeypatch, configured_root, job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(job.id, {"target": "output_folder"})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Job output folder is outside the configured download folder."
    }
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_rejects_completed_file_outside_job_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_root = tmp_path / "@creator" / "42"
    job_root.mkdir(parents=True)
    sibling_file = tmp_path / "other.md"
    sibling_file.write_text("other")
    job = make_reveal_job(tmp_path, completed_files=[str(sibling_file)])
    configure_reveal_database(monkeypatch, tmp_path, job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(
        job.id,
        {"target": "completed_file", "completed_file_index": 0},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Reveal target is outside the job output folder."
    }
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_root = tmp_path / "downloads" / "@creator" / "42"
    job_root.mkdir(parents=True)
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside")
    symlink = job_root / "post.md"
    symlink.symlink_to(outside_file)
    job = make_reveal_job(
        tmp_path / "downloads",
        output_dir=job_root,
        completed_files=[str(symlink)],
    )
    configure_reveal_database(monkeypatch, tmp_path / "downloads", job)
    _, popen = mock_explorer_processes(monkeypatch)

    response = await post_reveal(
        job.id,
        {"target": "completed_file", "completed_file_index": 0},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Reveal target is outside the configured download folder."
    }
    popen.assert_not_called()


@pytest.mark.parametrize("unavailable_process", ["wslpath", "explorer"])
@pytest.mark.asyncio
async def test_reveal_returns_service_unavailable_when_integration_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_process: str,
) -> None:
    job_root = tmp_path / "@creator" / "42"
    job_root.mkdir(parents=True)
    job = make_reveal_job(tmp_path)
    configure_reveal_database(monkeypatch, tmp_path, job)
    run, popen = mock_explorer_processes(monkeypatch)
    if unavailable_process == "wslpath":
        run.side_effect = FileNotFoundError
    else:
        popen.side_effect = FileNotFoundError

    response = await post_reveal(job.id, {"target": "output_folder"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Windows Explorer integration is unavailable."
    }


def selectable_analysis() -> Analysis:
    return Analysis(
        id="analysis-1",
        url="https://x.com/i/web/status/42",
        post=PostMetadata(post_id="42", author_name="Creator", author_handle="creator"),
        attachments=[
            Attachment(
                id="a-1",
                index=1,
                media_type=MediaType.VIDEO,
                extension="mp4",
                qualities=[
                    QualityOption(id="q-1080", label="1080p", selector="http-1080")
                ],
            )
        ],
    )


def test_selection_validation_rejects_duplicates_and_unknown_quality() -> None:
    analysis = selectable_analysis()
    duplicate = [
        AttachmentSelection(attachment_id="a-1"),
        AttachmentSelection(attachment_id="a-1"),
    ]
    with pytest.raises(HTTPException, match="selected once"):
        _validate_selections(analysis, duplicate)
    with pytest.raises(HTTPException, match="qualities"):
        _validate_selections(
            analysis,
            [AttachmentSelection(attachment_id="a-1", quality_id="q-fake")],
        )


def test_selection_key_treats_default_and_explicit_best_as_same() -> None:
    analysis = selectable_analysis()
    implicit = [AttachmentSelection(attachment_id="a-1")]
    explicit = [AttachmentSelection(attachment_id="a-1", quality_id="q-1080")]
    assert _selection_key(analysis, implicit) == _selection_key(analysis, explicit)


def test_document_outputs_allow_empty_media_selection() -> None:
    analysis = selectable_analysis()
    _validate_selections(analysis, [], [OutputFormat.MARKDOWN, OutputFormat.PDF])
    with pytest.raises(HTTPException, match="attachment"):
        _validate_selections(analysis, [], [OutputFormat.MEDIA])
    with pytest.raises(HTTPException, match="only be selected once"):
        _validate_selections(
            analysis, [], [OutputFormat.MARKDOWN, OutputFormat.MARKDOWN]
        )


def test_export_identity_includes_outputs_and_document_media() -> None:
    analysis = selectable_analysis()
    selections = [AttachmentSelection(attachment_id="a-1")]
    media = _selection_key(analysis, selections, [OutputFormat.MEDIA], True)
    markdown = _selection_key(analysis, selections, [OutputFormat.MARKDOWN], True)
    without_assets = _selection_key(
        analysis, selections, [OutputFormat.MARKDOWN], False
    )
    assert len({media, markdown, without_assets}) == 3


@pytest.mark.asyncio
async def test_completed_job_reuse_requires_verified_document_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = Analysis(
        id="analysis-text",
        url="https://x.com/i/web/status/42",
        post=PostMetadata(
            post_id="42", author_name="Creator", author_handle="creator", text="Post"
        ),
        attachments=[],
    )
    output_dir = tmp_path / "@creator" / "42"
    output_dir.mkdir(parents=True)
    markdown_path = output_dir / "post.md"
    markdown_path.write_bytes(b"expected")
    job = Job(
        id="job-text",
        analysis_id=analysis.id,
        url=analysis.url,
        post=analysis.post,
        selections=[],
        outputs=[OutputFormat.MARKDOWN],
        include_document_media=False,
        destination=str(tmp_path),
        layout_version=2,
        output_dir=str(output_dir),
    )
    monkeypatch.setattr(app_module, "render_markdown", lambda *_: b"expected")

    assert await _completed_job_is_reusable(job, analysis)

    markdown_path.write_bytes(b"different")
    assert not await _completed_job_is_reusable(job, analysis)
