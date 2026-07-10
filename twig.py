#!/usr/bin/env python3
"""twig"""

import hashlib
import fnmatch
import os
import sys
import time
import zlib
from collections import deque
from datetime import datetime

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

        ignore_patterns = _load_ignore_patterns()
        if _is_ignored(filepath, ignore_patterns):
            print(f"twig: '{filepath}' is ignored by .twigignore", file=sys.stderr)
            continue

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

    # Write reflog BEFORE updating the ref
    _reflog_write(branch, commit_hash, f"commit: {message}")

    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    os.makedirs(os.path.dirname(ref_path), exist_ok=True)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"[{branch} {commit_hash[:8]}] {message}")
    return commit_hash


def cmd_log(args):
    """Phase 5: Walk the commit history."""
    if "--graph" in args:
        return cmd_log_graph(args)

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


def parse_tree(tree_hash):
    """Parse a tree object into {filename: blob_hash} dict."""
    _, tree_data = read_object(tree_hash)
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
    return entries


def tree_from_commit(commit_hash):
    """Extract the tree hash from a commit object."""
    obj_type, data = read_object(commit_hash)
    if obj_type != "commit":
        return None
    for line in data.decode().split("\n"):
        if line.startswith("tree "):
            return line.split(" ", 1)[1]
    return None


def get_all_parents(commit_hash):
    """Get the parents of a single commit."""
    obj_type, data = read_object(commit_hash)
    if obj_type != "commit":
        return []
    parents = []
    for line in data.decode().split("\n"):
        if line.startswith("parent "):
            parents.append(line.split(" ", 1)[1])
    return parents


def lowest_common_ancestor(hash_a, hash_b):
    """Find the lowest common ancestor of two commits via BFS from both sides."""
    # Walk ancestors of hash_a, recording depth
    visited_a = {}
    queue = deque([(hash_a, 0)])
    while queue:
        h, depth = queue.popleft()
        if h in visited_a:
            continue
        visited_a[h] = depth
        for parent in get_all_parents(h):
            queue.append((parent, depth + 1))

    # BFS from hash_b, looking for first match in hash_a's ancestors
    queue = deque([(hash_b, 0)])
    visited_b = set()
    best = None
    best_depth_b = float("inf")

    while queue:
        h, depth_b = queue.popleft()
        if h in visited_b:
            continue
        visited_b.add(h)

        if h in visited_a:
            if best is None or depth_b < best_depth_b:
                best = h
                best_depth_b = depth_b
            # Don't stop early — keep looking for shallower match from b side
            continue

        for parent in get_all_parents(h):
            queue.append((parent, depth_b + 1))

    return best


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

    # Write reflog entry to TARGET branch BEFORE updating HEAD
    if os.path.exists(os.path.join(TWIG_DIR, "refs", "heads", target)):
        _reflog_write(target, commit_hash, f"checkout: moving to {commit_hash[:8]}")
    else:
        branch_name = get_current_branch()
        if branch_name:
            _reflog_write(branch_name, commit_hash, f"checkout: moving to {commit_hash[:8]}")

    # Update HEAD to point to the branch or detached commit
    head_path = os.path.join(TWIG_DIR, "HEAD")
    if os.path.exists(os.path.join(TWIG_DIR, "refs", "heads", target)):
        with open(head_path, "w") as f:
            f.write(f"ref: refs/heads/{target}\n")
    else:
        with open(head_path, "w") as f:
            f.write(commit_hash + "\n")

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


def lcs_diff(old_lines, new_lines):
    """Simple LCS-based diff between two lists of lines."""
    m, n = len(old_lines), len(new_lines)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old_lines[i - 1] == new_lines[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack to build the diff
    result = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and old_lines[i - 1] == new_lines[j - 1]:
            result.append((" ", old_lines[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            result.append(("+", new_lines[j - 1]))
            j -= 1
        else:
            result.append(("-", old_lines[i - 1]))
            i -= 1

    result.reverse()
    return result


def diff_trees(tree_a_hash, tree_b_hash):
    """Compare two tree objects and return diffs per file."""
    tree_a = parse_tree(tree_a_hash) if tree_a_hash else {}
    tree_b = parse_tree(tree_b_hash) if tree_b_hash else {}

    all_files = sorted(set(list(tree_a.keys()) + list(tree_b.keys())))
    diffs = []

    for filename in all_files:
        hash_a = tree_a.get(filename)
        hash_b = tree_b.get(filename)

        if hash_a == hash_b:
            continue

        if hash_a and not hash_b:
            diffs.append((filename, "deleted", None))
            continue

        if not hash_a and hash_b:
            _, data = read_object(hash_b)
            diffs.append((filename, "new", data.decode().splitlines()))
            continue

        _, data_a = read_object(hash_a)
        _, data_b = read_object(hash_b)
        lines_a = data_a.decode().splitlines()
        lines_b = data_b.decode().splitlines()

        if lines_a == lines_b:
            continue

        diff_result = lcs_diff(lines_a, lines_b)
        diffs.append((filename, "modified", diff_result))

    return diffs


def cmd_diff(args):
    """Phase 8: Compare working tree to last commit, or two commits."""
    if len(args) == 0:
        # Compare working tree vs HEAD commit
        commit_hash = get_current_commit()
        if not commit_hash:
            print("no commits yet", file=sys.stderr)
            sys.exit(1)

        tree_hash = tree_from_commit(commit_hash)
        entries = read_index()

        # Build a temp tree from index
        if entries:
            tree_content = b""
            for filepath, sha1 in sorted(entries.items()):
                mode = "100644"
                name = filepath.encode()
                tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"
            index_tree = hash_object(tree_content, obj_type="tree", write=False)
        else:
            index_tree = None

        diffs = diff_trees(tree_hash, index_tree)
        if not diffs:
            print("no changes")
            return

        for filename, change_type, data in diffs:
            print(f"--- {filename}")
            if change_type == "deleted":
                print("  (deleted)")
            elif change_type == "new":
                for line in data:
                    print(f"+ {line}")
            elif change_type == "modified":
                for op, line in data:
                    print(f"{op} {line}")

    elif len(args) == 2:
        # Compare two commits
        hash_a = args[0]
        hash_b = args[1]

        tree_a = tree_from_commit(hash_a)
        tree_b = tree_from_commit(hash_b)

        if not tree_a or not tree_b:
            print("twig: invalid commit hashes", file=sys.stderr)
            sys.exit(1)

        diffs = diff_trees(tree_a, tree_b)
        if not diffs:
            print("no differences")
            return

        for filename, change_type, data in diffs:
            print(f"--- {filename}")
            if change_type == "deleted":
                print("  (deleted)")
            elif change_type == "new":
                for line in data:
                    print(f"+ {line}")
            elif change_type == "modified":
                for op, line in data:
                    print(f"{op} {line}")
    else:
        print("usage: twig diff [<commit-a> <commit-b>]", file=sys.stderr)
        sys.exit(1)


def cmd_merge(args):
    """Phase 9: Merge a branch into the current branch."""
    if not args:
        print("usage: twig merge <branch>", file=sys.stderr)
        sys.exit(1)

    branch_name = args[0]
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch_name)
    if not os.path.exists(ref_path):
        print(f"twig: branch '{branch_name}' not found", file=sys.stderr)
        sys.exit(1)

    with open(ref_path, "r") as f:
        merge_hash = f.read().strip()

    current_hash = get_current_commit()
    if not current_hash:
        print("twig: no current commit", file=sys.stderr)
        sys.exit(1)

    if current_hash == merge_hash:
        print("Already up to date.")
        return

    lca = lowest_common_ancestor(current_hash, merge_hash)
    if not lca:
        print("twig: no common ancestor found", file=sys.stderr)
        sys.exit(1)

    lca_tree = tree_from_commit(lca) if lca != current_hash else tree_from_commit(current_hash)
    current_tree = tree_from_commit(current_hash)
    merge_tree = tree_from_commit(merge_hash)

    lca_entries = parse_tree(lca_tree) if lca_tree else {}
    current_entries = parse_tree(current_tree) if current_tree else {}
    merge_entries = parse_tree(merge_tree) if merge_tree else {}

    all_files = sorted(
        set(list(lca_entries.keys()) + list(current_entries.keys()) + list(merge_entries.keys()))
    )

    merged_entries = {}
    conflicts = []

    for filename in all_files:
        hash_lca = lca_entries.get(filename)
        hash_curr = current_entries.get(filename)
        hash_merge = merge_entries.get(filename)

        curr_changed = hash_curr != hash_lca
        merge_changed = hash_merge != hash_lca

        if curr_changed and merge_changed:
            if hash_curr != hash_merge:
                conflicts.append(filename)
                merged_entries[filename] = hash_curr
            else:
                merged_entries[filename] = hash_curr
        elif curr_changed:
            merged_entries[filename] = hash_curr
        elif merge_changed:
            merged_entries[filename] = hash_merge
        else:
            if hash_curr:
                merged_entries[filename] = hash_curr
            elif hash_merge:
                merged_entries[filename] = hash_merge

    if conflicts:
        print(f"CONFLICT (content merge conflict in {', '.join(conflicts)})")
        print("Automatic merge failed; fix conflicts and then commit the result.")
        return

    tree_content = b""
    for filepath, sha1 in sorted(merged_entries.items()):
        mode = "100644"
        name = filepath.encode()
        tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"

    merged_tree_hash = hash_object(tree_content, obj_type="tree", write=True)

    commit_content = f"tree {merged_tree_hash}\n"
    commit_content += f"parent {current_hash}\n"
    commit_content += f"parent {merge_hash}\n"
    commit_content += f"author twig <twig@local> {int(time.time())}\n"
    commit_content += f"\nMerge branch '{branch_name}'\n"

    commit_hash = hash_object(commit_content.encode(), obj_type="commit", write=True)

    current_branch = get_current_branch() or "main"
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", current_branch)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"Merge made by twig.")


def cmd_status(args):
    """Phase 10: Show working tree status — staged, unstaged, untracked."""
    commit_hash = get_current_commit()
    head_tree = tree_from_commit(commit_hash) if commit_hash else None
    head_entries = parse_tree(head_tree) if head_tree else {}
    index_entries = read_index()

    staged = []
    unstaged = []
    untracked = []

    # Check index vs HEAD
    for filepath, sha1 in sorted(index_entries.items()):
        if filepath not in head_entries:
            staged.append(("new", filepath))
        elif head_entries[filepath] != sha1:
            staged.append(("modified", filepath))

    # Deleted from index but was in HEAD
    for filepath in head_entries:
        if filepath not in index_entries:
            staged.append(("deleted", filepath))

    # Check working tree vs index
    for filepath, index_sha1 in sorted(index_entries.items()):
        if not os.path.exists(filepath):
            unstaged.append(("deleted", filepath))
            continue

        with open(filepath, "rb") as f:
            data = f.read()
        work_sha1 = hash_object(data, write=False)
        if work_sha1 != index_sha1:
            unstaged.append(("modified", filepath))

    # Check for untracked files
    all_tracked = set(list(head_entries.keys()) + list(index_entries.keys()))
    ignore_patterns = _load_ignore_patterns()
    for entry in os.listdir("."):
        if entry.startswith(".") or entry == "twig.py" or entry == "__pycache__":
            continue
        if os.path.isfile(entry) and entry not in all_tracked:
            if not _is_ignored(entry, ignore_patterns):
                untracked.append(entry)

    # Print output
    branch = get_current_branch() or "HEAD"
    print(f"On branch {branch}")

    if staged:
        print("\nChanges to be committed:")
        for op, filepath in staged:
            print(f"  {op}: {filepath}")

    if unstaged:
        print("\nChanges not staged for commit:")
        for op, filepath in unstaged:
            print(f"  {op}: {filepath}")

    if untracked:
        print("\nUntracked files:")
        for filepath in untracked:
            print(f"  {filepath}")

    if not staged and not unstaged and not untracked:
        print("nothing to commit, working tree clean")


def cmd_show(args):
    """Phase 11: Show commit details + diff."""
    if not args:
        commit_hash = get_current_commit()
    else:
        commit_hash = args[0]

    if not commit_hash:
        print("no commits yet", file=sys.stderr)
        sys.exit(1)

    obj_type, data = read_object(commit_hash)
    if obj_type != "commit":
        print(f"twig: '{commit_hash}' is not a commit", file=sys.stderr)
        sys.exit(1)

    content = data.decode()
    tree_hash = None
    parent_hashes = []
    author_line = ""
    message = ""

    for line in content.split("\n"):
        if line.startswith("tree "):
            tree_hash = line.split(" ", 1)[1]
        elif line.startswith("parent "):
            parent_hashes.append(line.split(" ", 1)[1])
        elif line.startswith("author "):
            author_line = line
        elif line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
            message = line

    # Parse timestamp from author line
    ts_str = ""
    if author_line:
        parts = author_line.rsplit(" ", 1)
        if len(parts) == 2:
            try:
                ts = int(parts[1])
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts_str = parts[1]

    print(f"commit {commit_hash}")
    print(f"Author: twig <twig@local>")
    if ts_str:
        print(f"Date:   {ts_str}")
    print(f"\n    {message}")

    # Show diff against parent
    if parent_hashes:
        parent_tree = tree_from_commit(parent_hashes[0])
        diffs = diff_trees(parent_tree, tree_hash)
    else:
        # First commit — show all files as new
        diffs = []
        entries = parse_tree(tree_hash) if tree_hash else {}
        for filepath, sha1 in sorted(entries.items()):
            _, blob_data = read_object(sha1)
            diffs.append((filepath, "new", blob_data.decode().splitlines()))

    if diffs:
        for filename, change_type, data_lines in diffs:
            print(f"\ndiff --a/{filename}")
            if change_type == "deleted":
                print(f"deleted file {filename}")
            elif change_type == "new":
                print(f"new file {filename}")
                for line in data_lines:
                    print(f"+{line}")
            elif change_type == "modified":
                for op, line in data_lines:
                    print(f"{op} {line}")


def cmd_cat_file(args):
    """Phase 12: Inspect raw objects (plumbing command)."""
    if not args:
        print("usage: twig cat-file <hash>", file=sys.stderr)
        sys.exit(1)

    sha1 = args[0]
    try:
        obj_type, data = read_object(sha1)
    except FileNotFoundError:
        print(f"twig: object '{sha1}' not found", file=sys.stderr)
        sys.exit(1)

    print(f"type: {obj_type}")
    print(f"size: {len(data)}")
    print("---")
    if obj_type == "blob":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.write(b"\n")
    else:
        print(data.decode(), end="")


def cmd_rm(args):
    """Phase 13: Remove files from the index (unstage)."""
    if not args:
        print("usage: twig rm <file> [<file> ...]", file=sys.stderr)
        sys.exit(1)

    entries = read_index()
    for filepath in args:
        if filepath not in entries:
            print(f"twig: '{filepath}' not in index", file=sys.stderr)
            continue
        del entries[filepath]
        print(f"removed {filepath} from index")

    write_index(entries)


def cmd_tag(args):
    """Phase 14: Create or list tags."""
    if not args:
        tags_dir = os.path.join(TWIG_DIR, "refs", "tags")
        if not os.path.exists(tags_dir):
            print("no tags yet")
            return
        for name in sorted(os.listdir(tags_dir)):
            ref_path = os.path.join(tags_dir, name)
            with open(ref_path, "r") as f:
                tag_hash = f.read().strip()
            print(f"{name} -> {tag_hash[:8]}")
        return

    tag_name = args[0]
    tag_path = os.path.join(TWIG_DIR, "refs", "tags", tag_name)
    if os.path.exists(tag_path):
        print(f"twig: tag '{tag_name}' already exists", file=sys.stderr)
        sys.exit(1)

    commit_hash = get_current_commit()
    if not commit_hash:
        print("twig: no commits yet", file=sys.stderr)
        sys.exit(1)

    # Store as tag object (lightweight — just a ref for now)
    # Full annotated tag would create a "tag" type object; keeping it simple
    os.makedirs(os.path.dirname(tag_path), exist_ok=True)
    with open(tag_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"created tag '{tag_name}' at {commit_hash[:8]}")


def cmd_reset(args):
    """Phase 15: Reset current branch to a commit.

    --soft:  move branch pointer only
    --mixed: move branch pointer + reset index (default)
    --hard:  move branch pointer + reset index + reset working tree
    """
    mode = "mixed"
    target = None

    for arg in args:
        if arg.startswith("--"):
            mode = arg[2:]
            if mode not in ("soft", "mixed", "hard"):
                print(f"twig: unknown reset mode '{arg}'", file=sys.stderr)
                sys.exit(1)
        else:
            target = arg

    if not target:
        print("usage: twig reset [--soft|--mixed|--hard] <commit>", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(os.path.join(TWIG_DIR, "refs", "heads", target)):
        with open(os.path.join(TWIG_DIR, "refs", "heads", target), "r") as f:
            commit_hash = f.read().strip()
    else:
        commit_hash = target

    obj_type, _ = read_object(commit_hash)
    if obj_type != "commit":
        print(f"twig: '{target}' is not a valid commit", file=sys.stderr)
        sys.exit(1)

    branch = get_current_branch()
    if not branch:
        print("twig: not on a branch (detached HEAD)", file=sys.stderr)
        sys.exit(1)

    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    if mode in ("mixed", "hard"):
        tree_hash = tree_from_commit(commit_hash)
        entries = parse_tree(tree_hash) if tree_hash else {}
        write_index(entries)

    if mode == "hard":
        tree_hash = tree_from_commit(commit_hash)
        entries = parse_tree(tree_hash) if tree_hash else {}
        for existing in os.listdir("."):
            if existing.startswith(".") or existing == "twig.py" or existing == "__pycache__":
                continue
            if os.path.isfile(existing) and existing not in entries:
                os.remove(existing)
        for filename, blob_hash in entries.items():
            _, blob_data = read_object(blob_hash)
            with open(filename, "wb") as f:
                f.write(blob_data)

    _reflog_write(branch, commit_hash, f"reset: moving to {commit_hash[:8]}")
    print(f"HEAD is now at {commit_hash[:8]} ({mode} reset)")


def _reflog_write(branch, new_hash, reason=""):
    """Append an entry to the reflog for a branch."""
    reflog_path = os.path.join(TWIG_DIR, "logs", "refs", "heads", branch)
    os.makedirs(os.path.dirname(reflog_path), exist_ok=True)

    old_hash = ""
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    if os.path.exists(ref_path):
        with open(ref_path, "r") as f:
            old_hash = f.read().strip()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{old_hash} {new_hash} twig <twig@local> {ts} {reason}\n"
    with open(reflog_path, "a") as f:
        f.write(entry)


def cmd_reflog(args):
    """Phase 21: Show the reflog (all HEAD movements)."""
    branch = get_current_branch()
    if not branch:
        print("twig: not on a branch", file=sys.stderr)
        sys.exit(1)

    reflog_path = os.path.join(TWIG_DIR, "logs", "refs", "heads", branch)
    if not os.path.exists(reflog_path):
        print("reflog is empty")
        return

    with open(reflog_path, "r") as f:
        entries = f.readlines()

    for i, entry in enumerate(reversed(entries)):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(" ", 4)
        if len(parts) >= 5:
            old_h, new_h = parts[0][:8], parts[1][:8]
            rest = parts[4]
            print(f"{i}: {old_h} -> {new_h} {rest}")


def cmd_stash(args):
    """Phase 16: Stash working directory and index changes."""
    subcmd = args[0] if args else "save"

    stash_dir = os.path.join(TWIG_DIR, "stash")
    os.makedirs(stash_dir, exist_ok=True)

    if subcmd == "save" or subcmd == "push":
        index_entries = read_index()
        commit_hash = get_current_commit()
        head_tree = tree_from_commit(commit_hash) if commit_hash else None
        head_entries = parse_tree(head_tree) if head_tree else {}

        has_changes = False
        stash_index = {}
        stash_files = {}

        for filepath, sha1 in index_entries.items():
            if filepath not in head_entries or head_entries[filepath] != sha1:
                stash_index[filepath] = sha1
                has_changes = True

        for filepath, index_sha1 in index_entries.items():
            if not os.path.exists(filepath):
                continue
            with open(filepath, "rb") as f:
                data = f.read()
            work_sha1 = hash_object(data, write=False)
            if work_sha1 != index_sha1:
                stash_files[filepath] = data
                has_changes = True

        for filepath in index_entries:
            if not os.path.exists(filepath):
                stash_files[filepath] = None
                has_changes = True

        if not has_changes:
            print("No local changes to save")
            return

        existing = sorted([int(f) for f in os.listdir(stash_dir) if f.isdigit()])
        stash_num = existing[-1] + 1 if existing else 0

        stash_path = os.path.join(stash_dir, str(stash_num))
        os.makedirs(stash_path, exist_ok=True)

        with open(os.path.join(stash_path, "index"), "w") as f:
            for filepath, sha1 in sorted(stash_index.items()):
                f.write(f"{filepath} {sha1}\n")

        files_dir = os.path.join(stash_path, "files")
        os.makedirs(files_dir, exist_ok=True)
        for filepath, data in stash_files.items():
            safe_name = filepath.replace("/", "_").replace("\\", "_")
            if data is None:
                with open(os.path.join(files_dir, safe_name + ".deleted"), "w") as f:
                    pass
            else:
                with open(os.path.join(files_dir, safe_name), "wb") as f:
                    f.write(data)

        with open(os.path.join(stash_path, "parent"), "w") as f:
            f.write(commit_hash or "")

        with open(os.path.join(stash_path, "branch"), "w") as f:
            f.write(get_current_branch() or "")

        branch_name = get_current_branch() or "main"
        msg = f"WIP on {branch_name}: {commit_hash[:8] if commit_hash else 'no commits'}"
        with open(os.path.join(stash_path, "message"), "w") as f:
            f.write(msg)

        if commit_hash:
            entries = parse_tree(head_tree) if head_tree else {}
            for filepath in list(index_entries.keys()):
                if filepath in entries:
                    _, blob_data = read_object(entries[filepath])
                    with open(filepath, "wb") as f:
                        f.write(blob_data)
                elif os.path.exists(filepath):
                    os.remove(filepath)

        write_index(head_entries)
        print(f"Saved working directory and index state {msg}")

    elif subcmd == "pop":
        existing = sorted([int(f) for f in os.listdir(stash_dir) if f.isdigit()])
        if not existing:
            print("No stash entries found")
            return
        _apply_stash(stash_num := existing[-1])
        import shutil
        shutil.rmtree(os.path.join(stash_dir, str(stash_num)))
        print(f"Dropped refs/stash@{{{stash_num}}}")

    elif subcmd == "apply":
        existing = sorted([int(f) for f in os.listdir(stash_dir) if f.isdigit()])
        if not existing:
            print("No stash entries found")
            return
        _apply_stash(existing[-1])
        print(f"Applied stash@{{{existing[-1]}}}")

    elif subcmd == "list":
        existing = sorted([int(f) for f in os.listdir(stash_dir) if f.isdigit()])
        if not existing:
            return
        for num in reversed(existing):
            stash_path = os.path.join(stash_dir, str(num))
            msg_path = os.path.join(stash_path, "message")
            if os.path.exists(msg_path):
                with open(msg_path, "r") as f:
                    msg = f.read().strip()
                print(f"stash@{{{num}}}: {msg}")

    elif subcmd == "drop":
        existing = sorted([int(f) for f in os.listdir(stash_dir) if f.isdigit()])
        if not existing:
            print("No stash entries found")
            return
        stash_num = existing[-1]
        import shutil
        shutil.rmtree(os.path.join(stash_dir, str(stash_num)))
        print(f"Dropped refs/stash@{{{stash_num}}}")

    elif subcmd == "clear":
        import shutil
        for f in os.listdir(stash_dir):
            p = os.path.join(stash_dir, f)
            if os.path.isdir(p):
                shutil.rmtree(p)
        print("All stash entries cleared")

    else:
        print("usage: twig stash [save|pop|apply|list|drop|clear]", file=sys.stderr)
        sys.exit(1)


def _apply_stash(stash_num):
    """Apply a stash entry to the working directory."""
    stash_path = os.path.join(TWIG_DIR, "stash", str(stash_num))
    if not os.path.exists(stash_path):
        print(f"stash@{{{stash_num}}} not found", file=sys.stderr)
        sys.exit(1)

    stash_index = {}
    index_path = os.path.join(stash_path, "index")
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        stash_index[parts[0]] = parts[1]

    files_dir = os.path.join(stash_path, "files")
    if os.path.exists(files_dir):
        for safe_name in os.listdir(files_dir):
            if safe_name.endswith(".deleted"):
                filepath = safe_name[:-8].replace("_", "/")
                if os.path.exists(filepath):
                    os.remove(filepath)
            else:
                filepath = safe_name.replace("_", "/")
                with open(os.path.join(files_dir, safe_name), "rb") as f:
                    data = f.read()
                with open(filepath, "wb") as f:
                    f.write(data)

    current_index = read_index()
    current_index.update(stash_index)
    write_index(current_index)


def cmd_rebase(args):
    """Phase 17: Rebase current branch onto another branch."""
    if not args:
        print("usage: twig rebase <upstream>", file=sys.stderr)
        sys.exit(1)

    upstream_name = args[0]
    upstream_ref = os.path.join(TWIG_DIR, "refs", "heads", upstream_name)
    if not os.path.exists(upstream_ref):
        print(f"twig: branch '{upstream_name}' not found", file=sys.stderr)
        sys.exit(1)

    with open(upstream_ref, "r") as f:
        upstream_hash = f.read().strip()

    current_hash = get_current_commit()
    branch = get_current_branch()

    if not current_hash or not branch:
        print("twig: no current commit", file=sys.stderr)
        sys.exit(1)

    if current_hash == upstream_hash:
        print("Already up to date.")
        return

    lca = lowest_common_ancestor(current_hash, upstream_hash)
    if not lca:
        print("twig: no common ancestor found", file=sys.stderr)
        sys.exit(1)

    commits_to_replay = []
    h = current_hash
    while h and h != lca:
        commits_to_replay.append(h)
        parents = get_all_parents(h)
        h = parents[0] if parents else None

    if not commits_to_replay:
        print("Nothing to rebase")
        return

    commits_to_replay.reverse()

    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    with open(ref_path, "w") as f:
        f.write(upstream_hash + "\n")

    for commit_hash in commits_to_replay:
        obj_type, data = read_object(commit_hash)
        if obj_type != "commit":
            continue

        content = data.decode()
        old_tree_hash = None
        message = ""
        for line in content.split("\n"):
            if line.startswith("tree "):
                old_tree_hash = line.split(" ", 1)[1]
            elif line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
                message = line

        old_entries = parse_tree(old_tree_hash) if old_tree_hash else {}

        # Get parent commit of source (to know what the base was)
        source_parents = get_all_parents(commit_hash)
        source_parent_entries = {}
        if source_parents:
            sp_tree = tree_from_commit(source_parents[0])
            source_parent_entries = parse_tree(sp_tree) if sp_tree else {}

        # Current HEAD tree
        parent_hash = get_current_commit()
        parent_tree = tree_from_commit(parent_hash) if parent_hash else None
        parent_entries = parse_tree(parent_tree) if parent_tree else {}

        # Apply diff: source_parent -> old_entries on top of parent_entries
        new_entries = dict(parent_entries)
        all_files = sorted(
            set(list(source_parent_entries.keys()) + list(old_entries.keys()))
        )

        for filepath in all_files:
            base_sha = source_parent_entries.get(filepath)
            new_sha = old_entries.get(filepath)

            if base_sha == new_sha:
                continue

            if new_sha and not base_sha:
                new_entries[filepath] = new_sha
            elif new_sha and base_sha:
                new_entries[filepath] = new_sha
            elif not new_sha and base_sha:
                new_entries.pop(filepath, None)

        tree_content = b""
        for filepath, sha1 in sorted(new_entries.items()):
            mode = "100644"
            name = filepath.encode()
            tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"

        new_tree_hash = hash_object(tree_content, obj_type="tree", write=True)

        commit_content = f"tree {new_tree_hash}\n"
        commit_content += f"parent {parent_hash}\n" if parent_hash else ""
        commit_content += f"author twig <twig@local> {int(time.time())}\n"
        commit_content += f"\n{message}\n"

        new_commit_hash = hash_object(commit_content.encode(), obj_type="commit", write=True)

        ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
        with open(ref_path, "w") as f:
            f.write(new_commit_hash + "\n")

    print(f"Successfully rebased {branch} onto {upstream_name}")


def cmd_cherry_pick(args):
    """Phase 18: Apply a single commit from another branch."""
    if not args:
        print("usage: twig cherry-pick <commit>", file=sys.stderr)
        sys.exit(1)

    source_hash = args[0]
    try:
        obj_type, data = read_object(source_hash)
    except FileNotFoundError:
        print(f"twig: commit '{source_hash}' not found", file=sys.stderr)
        sys.exit(1)

    if obj_type != "commit":
        print(f"twig: '{source_hash}' is not a commit", file=sys.stderr)
        sys.exit(1)

    content = data.decode()
    source_tree_hash = None
    source_parents = []
    message = ""
    for line in content.split("\n"):
        if line.startswith("tree "):
            source_tree_hash = line.split(" ", 1)[1]
        elif line.startswith("parent "):
            source_parents.append(line.split(" ", 1)[1])
        elif line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
            message = line

    source_entries = parse_tree(source_tree_hash) if source_tree_hash else {}
    source_parent_tree = tree_from_commit(source_parents[0]) if source_parents else None
    source_parent_entries = parse_tree(source_parent_tree) if source_parent_tree else {}

    current_hash = get_current_commit()
    current_tree = tree_from_commit(current_hash) if current_hash else None
    current_entries = parse_tree(current_tree) if current_tree else {}

    new_entries = dict(current_entries)
    all_files = sorted(
        set(list(source_parent_entries.keys()) + list(source_entries.keys()))
    )

    for filepath in all_files:
        base_sha = source_parent_entries.get(filepath)
        source_sha = source_entries.get(filepath)
        if base_sha == source_sha:
            continue
        if source_sha and not base_sha:
            new_entries[filepath] = source_sha
        elif source_sha and base_sha:
            new_entries[filepath] = source_sha
        elif not source_sha and base_sha:
            new_entries.pop(filepath, None)

    tree_content = b""
    for filepath, sha1 in sorted(new_entries.items()):
        mode = "100644"
        name = filepath.encode()
        tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"

    new_tree_hash = hash_object(tree_content, obj_type="tree", write=True)

    commit_content = f"tree {new_tree_hash}\n"
    if current_hash:
        commit_content += f"parent {current_hash}\n"
    commit_content += f"author twig <twig@local> {int(time.time())}\n"
    commit_content += f"\n{message} (cherry-picked)\n"

    new_commit_hash = hash_object(commit_content.encode(), obj_type="commit", write=True)

    branch = get_current_branch() or "main"
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", branch)
    with open(ref_path, "w") as f:
        f.write(new_commit_hash + "\n")

    print(f"[{branch} {new_commit_hash[:8]}] {message} (cherry-picked)")


def _collect_dag():
    """BFS from all branch tips to build the full DAG."""
    heads_dir = os.path.join(TWIG_DIR, "refs", "heads")
    if not os.path.exists(heads_dir):
        return {}

    dag = {}
    queue = deque()
    visited = set()

    for branch_name in sorted(os.listdir(heads_dir)):
        ref_path = os.path.join(heads_dir, branch_name)
        with open(ref_path, "r") as f:
            h = f.read().strip()
        if h not in visited:
            queue.append(h)
            visited.add(h)

    while queue:
        h = queue.popleft()
        if h in dag:
            continue

        obj_type, data = read_object(h)
        if obj_type != "commit":
            continue

        parents = []
        message = ""
        for line in data.decode().split("\n"):
            if line.startswith("parent "):
                parents.append(line.split(" ", 1)[1])
            elif line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
                message = line

        dag[h] = {"parents": parents, "message": message}

        for parent in parents:
            if parent not in visited:
                queue.append(parent)
                visited.add(parent)

    return dag


def cmd_log_graph(args):
    """Phase 19: Show commit history with ASCII DAG graph."""
    commit_hash = get_current_commit()
    if not commit_hash:
        print("no commits yet", file=sys.stderr)
        sys.exit(1)

    dag = _collect_dag()

    heads_dir = os.path.join(TWIG_DIR, "refs", "heads")
    hash_to_branch = {}
    if os.path.exists(heads_dir):
        for branch_name in os.listdir(heads_dir):
            ref_path = os.path.join(heads_dir, branch_name)
            with open(ref_path, "r") as f:
                h = f.read().strip()
            hash_to_branch[h] = branch_name

    visited = set()
    queue = deque([(commit_hash, "", True)])

    while queue:
        h, prefix, is_last = queue.popleft()
        if h in visited or h not in dag:
            continue
        visited.add(h)

        node = dag[h]
        branch_label = ""
        if h in hash_to_branch:
            branch_label = f" ({hash_to_branch[h]})"
        if h == commit_hash:
            branch_label += " <- HEAD"

        connector = prefix
        print(f"{connector}* {h[:8]}{branch_label}")
        print(f"{connector}  {node['message']}")

        for i, parent in enumerate(node["parents"]):
            if parent not in visited:
                is_last_parent = (i == len(node["parents"]) - 1)
                if len(node["parents"]) > 1 and i == 0:
                    next_prefix = prefix + "| "
                elif is_last_parent:
                    next_prefix = prefix
                else:
                    next_prefix = prefix + "| "
                queue.append((parent, next_prefix, is_last_parent))


def cmd_blame(args):
    """Phase 20: Show who last modified each line of a file."""
    if not args:
        print("usage: twig blame <file>", file=sys.stderr)
        sys.exit(1)

    filepath = args[0]
    commit_hash = get_current_commit()
    if not commit_hash:
        print("no commits yet", file=sys.stderr)
        sys.exit(1)

    # Walk all commits, find the most recent one that changed each line
    # Build a list of (commit_hash, message) in reverse chronological order
    commits = []
    queue = deque([commit_hash])
    visited = set()
    while queue:
        h = queue.popleft()
        if h in visited:
            continue
        visited.add(h)
        obj_type, data = read_object(h)
        if obj_type != "commit":
            continue

        content = data.decode()
        message = ""
        for line in content.split("\n"):
            if line.strip() and not any(line.startswith(p) for p in ["tree ", "parent ", "author "]):
                message = line
                break

        commits.append((h, message))
        for parent in get_all_parents(h):
            queue.append(parent)

    if not commits:
        print("no commits found")
        return

    # For each line, find earliest commit that has the same line
    current_tree = tree_from_commit(commit_hash)
    entries = parse_tree(current_tree) if current_tree else {}

    if filepath not in entries:
        print(f"twig: '{filepath}' not found in current commit", file=sys.stderr)
        sys.exit(1)

    _, blob_data = read_object(entries[filepath])
    current_lines = blob_data.decode().splitlines()

    # Get all trees for blame lookup
    commit_trees = {}
    for ch, _ in commits:
        th = tree_from_commit(ch)
        commit_trees[ch] = parse_tree(th) if th else {}

    # For each line, walk back to find who introduced it
    blame_result = []
    for line in current_lines:
        blame_commit = commits[0][0]
        blame_msg = commits[0][1]

        for ch, msg in commits:
            ch_entries = commit_trees.get(ch, {})
            if filepath in ch_entries:
                _, ch_data = read_object(ch_entries[filepath])
                ch_lines = ch_data.decode().splitlines()
                if line in ch_lines:
                    blame_commit = ch
                    blame_msg = msg
                    break

        blame_result.append((blame_commit[:8], line, blame_msg))

        for ch_hash, line, msg in blame_result:
            print(f"{ch_hash} | {line}  ({msg})")


def cmd_config(args):
    """Phase 22: Get/set user configuration."""
    config_path = os.path.join(TWIG_DIR, "config")

    if not args or args[0] == "--list":
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                print(f.read(), end="")
        else:
            print("no configuration set")
        return

    if len(args) == 1:
        key = args[0]
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + " = "):
                        print(line.split(" = ", 1)[1])
                        return
        print(f"key '{key}' not found")
        return

    if args[0] == "--unset":
        key = args[1]
        if os.path.exists(config_path):
            lines = []
            with open(config_path, "r") as f:
                for line in f:
                    if not line.strip().startswith(key + " = "):
                        lines.append(line)
            with open(config_path, "w") as f:
                f.writelines(lines)
            print(f"key '{key}' removed")
        return

    key = args[0]
    value = args[1] if len(args) > 1 else ""

    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if " = " in line:
                    k, v = line.split(" = ", 1)
                    config[k] = v

    config[key] = value

    with open(config_path, "w") as f:
        for k, v in sorted(config.items()):
            f.write(f"{k} = {v}\n")

    print(f"{key} = {value}")


def _load_ignore_patterns():
    """Load ignore patterns from .twigignore."""
    patterns = []
    ignore_path = ".twigignore"
    if os.path.exists(ignore_path):
        with open(ignore_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def _is_ignored(filepath, patterns):
    """Check if a filepath matches any ignore pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(filepath, pattern):
            return True
        if fnmatch.fnmatch(os.path.basename(filepath), pattern):
            return True
    return False


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
        "diff": lambda: cmd_diff(args),
        "merge": lambda: cmd_merge(args),
        "status": lambda: cmd_status(args),
        "show": lambda: cmd_show(args),
        "cat-file": lambda: cmd_cat_file(args),
        "rm": lambda: cmd_rm(args),
        "tag": lambda: cmd_tag(args),
        "reset": lambda: cmd_reset(args),
        "stash": lambda: cmd_stash(args),
        "rebase": lambda: cmd_rebase(args),
        "cherry-pick": lambda: cmd_cherry_pick(args),
        "reflog": lambda: cmd_reflog(args),
        "blame": lambda: cmd_blame(args),
        "config": lambda: cmd_config(args),
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
