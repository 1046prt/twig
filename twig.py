#!/usr/bin/env python3
"""twig — A minimal version control system built from scratch."""

import hashlib
import os
import sys
import time
import zlib

TWIG_DIR = ".twig"


def init():
    """Phase 1: Initialize the repository structure."""
    dirs = [f"{TWIG_DIR}/objects", f"{TWIG_DIR}/refs/heads"]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    head_path = os.path.join(TWIG_DIR, "HEAD")
    if not os.path.exists(head_path):
        with open(head_path, "w") as f:
            f.write("ref: refs/heads/main\n")

    index_path = os.path.join(TWIG_DIR, "index")
    if not os.path.exists(index_path):
        with open(index_path, "w") as f:
            pass

    print(f"Initialized empty twig repository in {os.path.abspath(TWIG_DIR)}")


def hash_object(data, obj_type="blob", write=True):
    """Phase 2: Content-addressable object storage."""
    if isinstance(data, str):
        data = data.encode()

    header = f"{obj_type} {len(data)}\0".encode()
    full_data = header + data
    sha1 = hashlib.sha1(full_data).hexdigest()

    if write:
        obj_path = os.path.join(TWIG_DIR, "objects", sha1[:2], sha1[2:])
        os.makedirs(os.path.dirname(obj_path), exist_ok=True)
        with open(obj_path, "wb") as f:
            f.write(zlib.compress(full_data))

    return sha1


def read_object(sha1):
    """Read and decompress an object from the object store."""
    obj_path = os.path.join(TWIG_DIR, "objects", sha1[:2], sha1[2:])
    with open(obj_path, "rb") as f:
        raw = zlib.decompress(f.read())

    null_idx = raw.index(b"\0")
    header = raw[:null_idx].decode()
    obj_type, size = header.split(" ", 1)
    data = raw[null_idx + 1:]

    return obj_type, data


def read_index():
    """Read the staging area index file."""
    index_path = os.path.join(TWIG_DIR, "index")
    entries = {}
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    entries[parts[0]] = parts[1]
    return entries


def write_index(entries):
    """Write the staging area index file."""
    index_path = os.path.join(TWIG_DIR, "index")
    with open(index_path, "w") as f:
        for filepath, sha1 in sorted(entries.items()):
            f.write(f"{filepath} {sha1}\n")


def cmd_add(args):
    """Phase 3: Stage files for the next commit."""
    if not args:
        print("usage: twig add <file> [<file> ...]", file=sys.stderr)
        sys.exit(1)

    entries = read_index()

    for filepath in args:
        if not os.path.exists(filepath):
            print(f"twig: path '{filepath}' does not exist", file=sys.stderr)
            sys.exit(1)

        with open(filepath, "rb") as f:
            data = f.read()

        sha1 = hash_object(data, obj_type="blob", write=True)
        entries[filepath] = sha1
        print(f"staged {filepath} ({sha1[:8]})")

    write_index(entries)


def build_tree_from_index():
    """Phase 4a: Build a tree object from the current index.

    Tree format: lines of "mode filename\0hash" packed together.
    """
    entries = read_index()
    if not entries:
        return None

    tree_content = b""
    for filepath, sha1 in sorted(entries.items()):
        mode = "100644"
        name = filepath.encode()
        tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"

    tree_hash = hash_object(tree_content, obj_type="tree", write=True)
    return tree_hash


def get_current_branch():
    """Read which branch HEAD points to."""
    head_path = os.path.join(TWIG_DIR, "HEAD")
    with open(head_path, "r") as f:
        content = f.read().strip()

    if content.startswith("ref: "):
        ref = content[5:]
        return ref.replace("refs/heads/", "")
    return None


def get_current_commit():
    """Get the hash of the current branch's HEAD commit."""
    branch = get_current_branch()
    if not branch:
        return None

    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    if not os.path.exists(ref_path):
        return None

    with open(ref_path, "r") as f:
        return f.read().strip()


def cmd_commit(args):
    """Phase 4b: Create a commit from staged changes."""
    message = None
    for i, arg in enumerate(args):
        if arg == "-m" and i + 1 < len(args):
            message = args[i + 1]
            break

    if not message:
        message = "no message"

    tree_hash = build_tree_from_index()
    if not tree_hash:
        print("nothing to commit (empty index)", file=sys.stderr)
        sys.exit(1)

    parent_hash = get_current_commit()

    commit_content = f"tree {tree_hash}\n"
    if parent_hash:
        commit_content += f"parent {parent_hash}\n"
    commit_content += f"author twig <twig@local> {int(time.time())}\n"
    commit_content += f"\n{message}\n"

    commit_hash = hash_object(commit_content.encode(), obj_type="commit", write=True)

    branch = get_current_branch() or "main"
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    os.makedirs(os.path.dirname(ref_path), exist_ok=True)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"[{branch} {commit_hash[:8]}] {message}")
    return commit_hash


def cmd_log(args):
    """Phase 5: Walk the commit history."""
    commit_hash = get_current_commit()
    if not commit_hash:
        print("no commits yet", file=sys.stderr)
        sys.exit(1)

    while commit_hash:
        obj_type, data = read_object(commit_hash)
        if obj_type != "commit":
            break

        content = data.decode()
        lines = content.split("\n")

        parent_hash = None
        message = ""
        for line in lines:
            if line.startswith("parent "):
                parent_hash = line.split(" ", 1)[1]
            elif line.startswith("tree "):
                pass
            elif line.startswith("author "):
                pass
            elif line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
                message = line

        print(f"commit {commit_hash}")
        print(f"    {message}\n")

        commit_hash = parent_hash


def cmd_checkout(args):
    """Phase 6: Restore files from a commit (or branch)."""
    if not args:
        print("usage: twig checkout <commit-hash>", file=sys.stderr)
        sys.exit(1)

    target = args[0]

    if not os.path.exists(os.path.join(TWIG_DIR, "refs", "heads", target)):
        commit_hash = target
    else:
        ref_path = os.path.join(TWIG_DIR, "refs", "heads", target)
        with open(ref_path, "r") as f:
            commit_hash = f.read().strip()

    obj_type, data = read_object(commit_hash)
    if obj_type != "commit":
        print(f"twig: '{target}' is not a valid commit", file=sys.stderr)
        sys.exit(1)

    content = data.decode()
    tree_hash = None
    for line in content.split("\n"):
        if line.startswith("tree "):
            tree_hash = line.split(" ", 1)[1]
            break

    if not tree_hash:
        print("twig: commit has no tree", file=sys.stderr)
        sys.exit(1)

    tree_type, tree_data = read_object(tree_hash)

    entries = {}
    for line in tree_data.decode().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\0")
        if len(parts) == 2:
            mode_name = parts[0]
            sha1 = parts[1].strip()
            name = mode_name.split(" ", 1)[1]
            entries[name] = sha1

    for filename, blob_hash in entries.items():
        blob_type, blob_data = read_object(blob_hash)
        with open(filename, "wb") as f:
            f.write(blob_data)
        print(f"restored {filename}")

    print(f"switched to {target}")


def cmd_branch(args):
    """Phase 7: Create or list branches."""
    if not args:
        heads_dir = os.path.join(TWIG_DIR, "refs", "heads")
        if not os.path.exists(heads_dir):
            print("no branches yet")
            return

        current = get_current_branch()
        for name in sorted(os.listdir(heads_dir)):
            marker = "* " if name == current else "  "
            print(f"{marker}{name}")
        return

    branch_name = args[0]
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch_name)
    if os.path.exists(ref_path):
        print(f"twig: branch '{branch_name}' already exists", file=sys.stderr)
        sys.exit(1)

    commit_hash = get_current_commit()
    if not commit_hash:
        print("twig: no commits yet — cannot create branch", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(ref_path), exist_ok=True)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"created branch '{branch_name}' at {commit_hash[:8]}")


def main():
    if len(sys.argv) < 2:
        print("usage: twig <command> [<args>]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "init": lambda: init(),
        "hash-object": lambda: cmd_hash_object(args),
        "add": lambda: cmd_add(args),
        "commit": lambda: cmd_commit(args),
        "log": lambda: cmd_log(args),
        "checkout": lambda: cmd_checkout(args),
        "branch": lambda: cmd_branch(args),
    }

    if command in commands:
        commands[command]()
    else:
        print(f"twig: '{command}' is not a twig command.", file=sys.stderr)
        sys.exit(1)


def cmd_hash_object(args):
    """Standalone hash-object command."""
    if not args:
        print("usage: twig hash-object <file>", file=sys.stderr)
        sys.exit(1)

    filepath = args[0]
    with open(filepath, "rb") as f:
        data = f.read()

    sha1 = hash_object(data, obj_type="blob", write=True)
    print(sha1)


if __name__ == "__main__":
    main()
