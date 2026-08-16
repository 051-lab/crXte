from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .models import ThreadAnalysis, ThreadIssue, ThreadMember

if TYPE_CHECKING:
    from .models import Analysis

Fetcher = Callable[[str], Awaitable["Analysis"]]

THREAD_NO_ANCESTORS = "THREAD_NO_ANCESTORS"
THREAD_FOREIGN_PARENT = "THREAD_FOREIGN_PARENT"
THREAD_PARENT_UNAVAILABLE = "THREAD_PARENT_UNAVAILABLE"
THREAD_PARENT_MISMATCH = "THREAD_PARENT_MISMATCH"
THREAD_CYCLE = "THREAD_CYCLE"
THREAD_LIMIT_REACHED = "THREAD_LIMIT_REACHED"
THREAD_REPLIES_EXIST = "THREAD_REPLIES_EXIST"

DEFAULT_MAX_MEMBERS = 20


async def _default_fetch(url: str) -> Analysis:
    from .extractors import analyze_url

    return await analyze_url(url)


def _reply_to_post_id(analysis: Analysis) -> str | None:
    value = analysis.post.reply_to_post_id
    return str(value) if value else None


def _same_author(left: Analysis, right: Analysis) -> bool:
    return left.post.author_handle.lower() == right.post.author_handle.lower()


async def build_thread(
    focal: Analysis,
    *,
    fetch: Fetcher | None = None,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> ThreadAnalysis:
    """Collect the focal post plus its same-author ancestors.

    Discovery is strictly upward: each member's ``reply_to_post_id`` is
    fetched as a single public post.  Replies *below* the focal post cannot
    be enumerated without login cookies, so the returned thread only ever
    contains the focal post and posts that came before it.
    """
    fetch = fetch or _default_fetch
    members: list[Analysis] = [focal]
    seen = {focal.post.post_id}
    issues: list[ThreadIssue] = []
    current = focal
    parent_id = _reply_to_post_id(current)
    if parent_id is None:
        issues.append(
            ThreadIssue(
                code=THREAD_NO_ANCESTORS,
                message="This post is not a reply, so it has no author-thread ancestors.",
            )
        )
    while parent_id is not None:
        if len(members) >= max_members:
            issues.append(
                ThreadIssue(
                    code=THREAD_LIMIT_REACHED,
                    message=(
                        f"The thread was capped at {max_members} posts; "
                        "older replies were not fetched."
                    ),
                )
            )
            break
        if parent_id in seen:
            issues.append(
                ThreadIssue(
                    code=THREAD_CYCLE,
                    message=f"Post {parent_id} is already part of the thread; stopping.",
                )
            )
            break
        reply_to_author = current.post.reply_to_author
        if reply_to_author and reply_to_author.lower() != focal.post.author_handle.lower():
            issues.append(
                ThreadIssue(
                    code=THREAD_FOREIGN_PARENT,
                    message=(
                        f"This post replies to @{reply_to_author}, not to the post author; "
                        "the thread stops here."
                    ),
                )
            )
            break
        seen.add(parent_id)
        try:
            parent = await fetch(f"https://x.com/i/web/status/{parent_id}")
        except Exception as error:
            issues.append(
                ThreadIssue(
                    code=THREAD_PARENT_UNAVAILABLE,
                    message=(
                        f"The parent post {parent_id} could not be fetched "
                        f"({type(error).__name__})."
                    ),
                )
            )
            break
        if not _same_author(parent, focal):
            issues.append(
                ThreadIssue(
                    code=THREAD_FOREIGN_PARENT,
                    message=(
                        f"Post {parent_id} was not written by @{focal.post.author_handle}; "
                        "the thread stops here."
                    ),
                )
            )
            break
        if parent.post.post_id != parent_id:
            issues.append(
                ThreadIssue(
                    code=THREAD_PARENT_MISMATCH,
                    message=(
                        f"X returned post {parent.post.post_id} for the requested parent "
                        f"{parent_id}; the thread stops here."
                    ),
                )
            )
            break
        members.insert(0, parent)
        current = parent
        parent_id = _reply_to_post_id(current)
    if focal.post.reply_count:
        issues.append(
            ThreadIssue(
                code=THREAD_REPLIES_EXIST,
                message=(
                    f"This post has {focal.post.reply_count} replies on X that cannot be "
                    "enumerated without login cookies."
                ),
            )
        )
    return ThreadAnalysis(
        focal_post_id=focal.post.post_id,
        root_post_id=members[0].post.post_id,
        author_handle=focal.post.author_handle,
        members=[
            ThreadMember(
                post_id=item.post.post_id,
                url=item.url,
                analysis=item.model_copy(update={"thread": None}),
            )
            for item in members
        ],
        issues=issues,
    )