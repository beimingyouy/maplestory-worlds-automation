#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Restore missing remote-tracking refs by writing them into .git/packed-refs.

Why this exists
---------------
On this machine git cannot create loose refs under ``.git/refs/remotes/origin/``
(``git fetch`` / ``git update-ref`` exit 0 but the files are rolled back), so
``git status -sb`` keeps reporting ``[gone]`` even though the branch exists on
the remote and ``git ls-remote`` can see it.

Packing the refs sidesteps the problem: ``packed-refs`` is a single file write
that git performs normally, and ``git fetch`` afterwards keeps it intact.

Usage
-----
    python tools/fix_git_upstream.py            # fix and verify
    python tools/fix_git_upstream.py --check    # report only, change nothing

The script is ASCII-safe in its own source and prints UTF-8; run it with a
Python interpreter rather than through PowerShell string interpolation so the
Chinese branch name is not mangled by the console code page.
"""

from __future__ import annotations

import os
import subprocess
import sys

GIT_CANDIDATES = (
    r"C:\Program Files\Git\cmd\git.exe",
    "git",
)


def find_git() -> str:
    for candidate in GIT_CANDIDATES:
        if candidate == "git":
            return candidate
        if os.path.isfile(candidate):
            return candidate
    return "git"


def locate_root(start: str) -> str:
    """Walk upwards from this file until the .git directory is found."""
    current = os.path.dirname(os.path.abspath(start))
    for _ in range(8):
        if os.path.isdir(os.path.join(current, ".git")) or os.path.isfile(
            os.path.join(current, ".git")
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.dirname(os.path.abspath(start))


class Repo:
    def __init__(self, root: str, git: str) -> None:
        self.root = root
        self.git = git
        self.log: list[str] = []

    def run(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run(
            [self.git, *args],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        self.log.append("$ git " + " ".join(args) + "  [exit %d]" % proc.returncode)
        if text:
            self.log.append("  " + text.replace("\n", "\n  "))
        return proc.returncode, text

    def out(self, *args: str) -> str:
        return self.run(*args)[1]


def remote_branches(repo: Repo, remote: str = "origin") -> list[tuple[str, str]]:
    """Return [(branch, sha)] known on the remote, via ls-remote."""
    code, text = repo.run("ls-remote", "--heads", remote)
    if code != 0:
        return []
    result = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            result.append((ref[len(prefix) :], sha))
    return result


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    root = locate_root(__file__)
    git = find_git()
    repo = Repo(root, git)

    print("repo : %s" % root)
    print("git  : %s\n" % git)

    branch = repo.out("rev-parse", "--abbrev-ref", "HEAD")
    remotes = [line.strip() for line in repo.out("remote").splitlines() if line.strip()]
    if not remotes:
        print("No git remote configured; nothing to repair.")
        return 1
    remote = remotes[0]
    print("branch : %s" % branch)
    print("remote : %s\n" % remote)

    known = remote_branches(repo, remote)
    if not known:
        print("Could not read remote heads (auth or network problem).")
        print("\n".join(repo.log))
        return 1

    print("Remote branches:")
    for name, sha in known:
        print("  %-30s %s" % (name, sha[:10]))
    print("")

    existing = {}
    for line in repo.out(
        "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes"
    ).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            existing[parts[0].strip()] = parts[1].strip()

    missing = [
        (name, sha)
        for name, sha in known
        if ("%s/%s" % (remote, name)) not in existing
        or existing["%s/%s" % (remote, name)] != sha
    ]

    if not missing:
        print("All remote-tracking refs are already present and up to date.")
    else:
        print("Missing / stale refs:")
        for name, sha in missing:
            print("  %s/%s -> %s" % (remote, name, sha[:10]))
        print("")

    if check_only:
        print("--check given, no changes written.")
        return 0

    if missing:
        # Write loose refs first: `git pack-refs` only packs what git can see,
        # and git can read loose refs that we create ourselves.
        origin_dir = os.path.join(root, ".git", "refs", "remotes", remote)
        os.makedirs(origin_dir, exist_ok=True)
        for name, sha in missing:
            path = os.path.join(origin_dir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(sha + "\n")
        print("Wrote %d loose ref(s)." % len(missing))

        code, _ = repo.run("pack-refs", "--all")
        if code != 0:
            print("pack-refs failed; refs may not survive the next fetch.")
            return 1
        print("Packed refs into .git/packed-refs.")

    # Bind the current branch to its upstream without putting the (possibly
    # non-ASCII) branch name through the shell code page.
    repo.run("branch", "--set-upstream-to=%s/%s" % (remote, branch), branch)

    print("\nVerifying with a fresh fetch:")
    repo.run("fetch", remote)
    print(repo.out("for-each-ref", "--format=%(refname:short) %(objectname:short)", "refs/remotes"))
    print("")
    print(repo.out("status", "-sb"))
    print("")
    counts = repo.out(
        "rev-list", "--left-right", "--count", "%s/%s...HEAD" % (remote, branch)
    )
    parts = counts.split()
    if len(parts) == 2:
        print("behind=%s ahead=%s" % (parts[0], parts[1]))
    print("")
    print("Upstream link is healthy when status shows")
    print("  ## %s...%s/%s" % (branch, remote, branch))
    print("without a trailing [gone].")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
