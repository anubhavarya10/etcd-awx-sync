"""Git client for cloning/pulling/committing/pushing via gitpython."""

import os
import logging
from typing import Optional

import git

logger = logging.getLogger(__name__)

WORKSPACE_DIR = "/app/workspace"


class GitClient:
    """Manages the local clone of the vivox-ops-openstack repo."""

    def __init__(self):
        self.repo_url = os.environ.get(
            "TF_REPO_URL",
            "https://github.com/Unity-Technologies/vivox-ops-openstack.git",
        )
        self.branch = os.environ.get("TF_REPO_BRANCH", "main")
        self.repo_dir = os.path.join(WORKSPACE_DIR, "vivox-ops-openstack")
        self._repo: Optional[git.Repo] = None

    def _get_authenticated_url(self) -> str:
        """Build repo URL with embedded GitHub token."""
        token = os.environ.get("TF_GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        if not token:
            raise ValueError("TF_GITHUB_TOKEN environment variable is not set")

        # https://github.com/... -> https://<token>@github.com/...
        if self.repo_url.startswith("https://"):
            return self.repo_url.replace("https://", f"https://{token}@")
        return self.repo_url

    def _configure_repo(self, repo: git.Repo) -> None:
        """Configure git user for commits."""
        repo.config_writer().set_value("user", "name", "vivox-ops-bot").release()
        repo.config_writer().set_value("user", "email", "vivox-ops-bot@unity3d.com").release()

    def clone_or_pull(self) -> str:
        """
        Clone the repo if not present, or pull latest changes.

        Returns the repo directory path.
        """
        auth_url = self._get_authenticated_url()

        if os.path.exists(os.path.join(self.repo_dir, ".git")):
            logger.info(f"Pulling latest changes in {self.repo_dir}")
            self._repo = git.Repo(self.repo_dir)

            # Update remote URL in case token changed
            origin = self._repo.remotes.origin
            origin.set_url(auth_url)

            # Reset any local changes and pull
            self._repo.head.reset(index=True, working_tree=True)
            origin.pull(self.branch)
        else:
            logger.info(f"Cloning {self.repo_url} to {self.repo_dir}")
            os.makedirs(WORKSPACE_DIR, exist_ok=True)
            self._repo = git.Repo.clone_from(
                auth_url,
                self.repo_dir,
                branch=self.branch,
            )

        self._configure_repo(self._repo)
        logger.info(f"Repo ready at {self.repo_dir}, HEAD: {self._repo.head.commit.hexsha[:8]}")
        return self.repo_dir

    def commit_and_push(self, file_path: str, message: str) -> str:
        """
        Stage a file, commit with the given message, and push.

        Returns the commit hash.
        """
        if self._repo is None:
            raise RuntimeError("Repo not initialized - call clone_or_pull() first")

        # Stage the modified file
        rel_path = os.path.relpath(file_path, self.repo_dir)
        self._repo.index.add([rel_path])

        # Commit
        commit = self._repo.index.commit(message)
        commit_hash = commit.hexsha[:8]
        logger.info(f"Committed: {commit_hash} - {message}")

        # Push
        origin = self._repo.remotes.origin
        push_info = origin.push(self.branch)

        for info in push_info:
            if info.flags & info.ERROR:
                raise RuntimeError(f"Push failed: {info.summary}")

        logger.info(f"Pushed commit {commit_hash} to {self.branch}")
        return commit_hash

    def get_file_path(self, domain: str) -> str:
        """
        Get the path to the .tf file for a domain.

        Convention: production/<domain>/xmpp.tf
        """
        return os.path.join(self.repo_dir, "production", domain, "xmpp.tf")

    def read_file(self, file_path: str) -> str:
        """Read a file from the repo."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        with open(file_path, "r") as f:
            return f.read()

    def write_file(self, file_path: str, content: str) -> None:
        """Write content to a file in the repo."""
        with open(file_path, "w") as f:
            f.write(content)
