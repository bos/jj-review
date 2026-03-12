"""GitHub API response models."""

from pydantic import BaseModel, ConfigDict


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
