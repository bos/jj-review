"""GitHub API response models."""

from pydantic import BaseModel, ConfigDict, Field


class GithubRepository(BaseModel):
    """Subset of repository fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    clone_url: str
    default_branch: str
    full_name: str
    html_url: str
    name: str
    private: bool
    url: str


class GithubBranchRef(BaseModel):
    """Subset of branch-ref fields embedded in pull request payloads."""

    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    ref: str


class GithubPullRequest(BaseModel):
    """Subset of pull request fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    head: GithubBranchRef
    html_url: str
    is_draft: bool = Field(default=False, alias="draft")
    merged_at: str | None = None
    node_id: str | None = None
    number: int
    state: str
    title: str


class GithubPullRequestReviewUser(BaseModel):
    """Subset of review-author fields used to summarize PR reviews."""

    model_config = ConfigDict(extra="ignore")

    login: str


class GithubPullRequestReview(BaseModel):
    """Subset of PR review fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    id: int
    state: str
    user: GithubPullRequestReviewUser | None = None


class GithubIssueComment(BaseModel):
    """Subset of issue-comment fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    body: str
    html_url: str
    id: int
