#!/usr/bin/env python3
"""twig"""

import hashlib
import os
import sys
import time
import zlib
from collections import deque

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
                merged_entries[filename] = hash_curr  # keep current for now
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

    # Build merged tree
    tree_content = b""
    for filepath, sha1 in sorted(merged_entries.items()):
        mode = "100644"
        name = filepath.encode()
        tree_content += f"{mode} ".encode() + name + b"\0" + sha1.encode() + b"\n"

    merged_tree_hash = hash_object(tree_content, obj_type="tree", write=True)

    # Create merge commit with two parents
    commit_content = f"tree {merged_tree_hash}\n"
    commit_content += f"parent {current_hash}\n"
    commit_content += f"parent {merge_hash}\n"
    commit_content += f"author twig <twig@local> {int(time.time())}\n"
    commit_content += f"\nMerge branch '{branch_name}'\n"

    commit_hash = hash_object(commit_content.encode(), obj_type="commit", write=True)

    # Update current branch
    current_branch = get_current_branch() or "main"
    ref_path = os.path.join(TWIG_DIR, "refs", "heads", current_branch)
    with open(ref_path, "w") as f:
        f.write(commit_hash + "\n")

    print(f"Merge made by twig.")


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
