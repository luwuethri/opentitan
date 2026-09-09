#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Slang lint runner — reads a dvsim hjson lint config and generates file lists.

Optionally runs slang via oseda on the generated .f files.

Usage:
  # All IPs in the earlgrey lint config:
  python3 util/slang_lint.py hw/top_earlgrey/lint/top_earlgrey_lint_cfgs.hjson

  # Single IP (--select-cfgs uart):
  python3 util/slang_lint.py hw/top_earlgrey/lint/top_earlgrey_lint_cfgs.hjson \\
      --select-cfgs uart

  # Generate files and run slang:
  python3 util/slang_lint.py hw/top_earlgrey/lint/top_earlgrey_lint_cfgs.hjson \\
      --select-cfgs uart --run

  # Purge existing output first (--purge):
  python3 util/slang_lint.py hw/top_earlgrey/lint/top_earlgrey_lint_cfgs.hjson \\
      --purge --run
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import hjson
from fusesoc.coremanager import DependencyError

from fusesoc_export import _collect, edam_to_flist, resolve
from reggen.version import show_and_exit


def _parse_mappings(additional_arg: str) -> list:
    """Extract --mapping values from an additional_fusesoc_argument string."""
    return re.findall(r"--mapping[= ]?(\S+)", additional_arg)


def _toplevel_list(edam: dict) -> list:
    """Return the toplevel as a list, handling both string and list EDAM formats."""
    toplevel = edam.get("toplevel", [])
    if isinstance(toplevel, str):
        return [toplevel] if toplevel else []
    return toplevel


def generate(core_name: str,
             mappings: list,
             repo_root: str,
             build_root: str,
             purge: bool = False):
    """Resolve core, optionally purge, and write a .f file to build/<core>/flist/.

    Returns (flist_path, toplevel_list).
    """
    edam, work_root, sanitized = resolve(core_name, mappings, repo_root)

    out_dir = Path(build_root) / sanitized / "flist"

    if purge and out_dir.exists():
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    flist_path = out_dir / f"{sanitized}.f"

    files, include_dirs, defines = _collect(edam, work_root)
    flist_path.write_text(edam_to_flist(files, include_dirs, defines))
    toplevel = _toplevel_list(edam)
    top_str = f", top: {' '.join(toplevel)}" if toplevel else ""
    print(f"  wrote {out_dir.relative_to(repo_root)}"
          f" ({len(files)} files, {len(include_dirs)} include dirs{top_str})")

    return flist_path, toplevel


def run_slang(flist_path: Path, toplevel: list) -> bool:
    """Run oseda slang on flist_path. Returns True on success."""
    cmd = [
        "oseda",
        "slang",
        "-f",
        str(flist_path),
        "--ignore-unknown-modules",
        "--error-limit",
        "0",
    ]
    if toplevel:
        cmd += ["--top", toplevel[0]]
    print(f"  {' '.join(cmd)}")
    return subprocess.run(cmd).returncode == 0


def load_cfg(cfg_path: Path, proj_root: str) -> dict:
    text = cfg_path.read_text().replace("{proj_root}", proj_root)
    return hjson.loads(text)


def main():
    parser = argparse.ArgumentParser(
        description="Slang lint runner (dvsim-compatible hjson → build/)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version",
                        action="store_true",
                        help="Show version and exit")
    parser.add_argument(
        "cfg",
        nargs="?",
        help=
        "hjson lint config, e.g. hw/top_earlgrey/lint/top_earlgrey_lint_cfgs.hjson",
    )
    parser.add_argument(
        "--select-cfgs",
        metavar="NAME[,NAME...]",
        default=None,
        help="Comma-separated cfg names to process (default: all)",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete build/<core>/flist/ before regenerating",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run slang after generating file lists (requires oseda on PATH)",
    )
    parser.add_argument(
        "--build-root",
        default="build",
        help="Build directory root (default: build/)",
    )
    parser.add_argument(
        "--cores-root",
        default=".",
        help="Repo root for FuseSoC core discovery (default: .)",
    )
    args = parser.parse_args()

    if args.version:
        show_and_exit(__file__, ["fusesoc", "hjson"])

    if not args.cfg:
        parser.error("the following arguments are required: cfg")

    repo_root = os.path.abspath(args.cores_root)

    cfg_path = Path(args.cfg)
    if not cfg_path.is_absolute():
        cfg_path = Path(repo_root) / cfg_path

    cfg = load_cfg(cfg_path, repo_root)
    entries = cfg.get("use_cfgs", [])

    if args.select_cfgs:
        selected = {s.strip() for s in args.select_cfgs.split(",")}
        entries = [e for e in entries if e.get("name") in selected]
        if not entries:
            print(f"error: no cfgs matched {selected}", file=sys.stderr)
            sys.exit(1)

    build_root = os.path.join(repo_root, args.build_root)
    passed, failed = 0, 0

    for entry in entries:
        name = entry.get("name", "?")
        core_name = entry.get("fusesoc_core")
        if not core_name:
            print(f"[{name}] skipping: no fusesoc_core")
            continue

        mappings = _parse_mappings(entry.get("additional_fusesoc_argument",
                                             ""))

        print(f"[{name}] {core_name}")
        try:
            flist_path, toplevel = generate(core_name,
                                            mappings,
                                            repo_root,
                                            build_root,
                                            purge=args.purge)
        except (RuntimeError, DependencyError) as e:
            print(f"  error: {e}", file=sys.stderr)
            failed += 1
            continue

        if args.run:
            if run_slang(flist_path, toplevel):
                passed += 1
            else:
                failed += 1
        else:
            passed += 1

    total = passed + failed
    print(f"\n{'slang ' if args.run else ''}results: {passed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
