#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Generate a .f file or Bender.yml from a FuseSoC core + target.

Resolves the dependency graph for the given core and target using the
FuseSoC Python API, then writes either:
  - flist (default): a .f file for direct use with slang/verilator/etc.,
                     written to build/<core>/flist/<core>.f
  - bender:          a Bender.yml for use with `bender script flist-plus`,
                     written to Bender.yml in the repo root

The --mapping flag (repeatable) selects technology-specific implementations
for virtual cores, producing a deterministic file list. Without it, FuseSoC
picks arbitrarily among equally-valid candidates and warns.

How to use:
    # .f file for slang (default format, earlgrey technology):
    python3 util/fusesoc_export.py lowrisc:ip:uart \\
        --mapping lowrisc:systems:top_earlgrey:0.1

    # Bender file instead:
    python3 util/fusesoc_export.py lowrisc:ip:uart \\
        --mapping lowrisc:systems:top_earlgrey:0.1 \\
        --format bender

    # All three tops (generates .f files under build/):
    for top in earlgrey darjeeling englishbreakfast; do
        python3 util/fusesoc_export.py lowrisc:systems:top_${top} \\
            --mapping lowrisc:systems:top_${top}:0.1
    done
"""

import argparse
import os
import sys
from pathlib import Path

import yaml
from fusesoc.config import Config
from fusesoc.coremanager import DependencyError
from fusesoc.edalizer import Edalizer
from fusesoc.fusesoc import Fusesoc

from reggen.version import show_and_exit

SV_FILE_TYPES = {"systemVerilogSource", "verilogSource"}


def _abs(work_root: str, rel: str) -> str:
    return os.path.normpath(os.path.join(work_root, rel))


def _collect(edam: dict, work_root: str):
    """Return (files, include_dirs, defines) as lists of absolute-path strings."""
    files = []
    include_dirs = list(dict.fromkeys(edam.get("include_dirs", [])))

    for f in edam.get("files", []):
        if f.get("file_type") not in SV_FILE_TYPES:
            continue
        abs_path = _abs(work_root, f["name"])
        if f.get("is_include_file"):
            parent = str(Path(abs_path).parent)
            if parent not in include_dirs:
                include_dirs.append(parent)
        else:
            files.append(abs_path)

    defines = []
    for k, v in edam.get("vlogdefine", {}).items():
        defines.append(f"{k}={v}" if v is not None else k)

    return files, include_dirs, defines


def edam_to_bender(edam: dict, files: list, include_dirs: list,
                   defines: list) -> dict:
    """Convert collected EDAM data to the Bender YAML structure."""
    source_entry = {"files": files}
    if include_dirs:
        source_entry["include_dirs"] = include_dirs

    package_name = (edam.get("name", "unknown").replace(":", "_").replace(
        ".", "_").replace("-", "_"))

    bender = {
        "package": {
            "name": package_name
        },
        "sources": [source_entry],
    }

    if defines:
        bender["package"]["metadata"] = {"defines": defines}

    return bender


def edam_to_flist(files: list, include_dirs: list, defines: list) -> str:
    """Convert collected EDAM data to a .f (command file) for slang/verilator.

    Format:
        +incdir+/abs/path/to/dir    include directory
        +define+FOO=BAR             verilog define
        /abs/path/to/file.sv        source file
    """
    lines = []
    for d in include_dirs:
        lines.append(f"+incdir+{d}")
    for d in defines:
        lines.append(f"+define+{d}")
    lines.extend(files)
    return "\n".join(lines) + "\n"


def resolve(
    core_name: str,
    mappings: list,
    repo_root: str,
    target: str = "lint",
) -> tuple:
    """Resolve a FuseSoC core dependency graph.

    Returns (edam, work_root, sanitized_name).
    Changes the process working directory to repo_root as a side effect.
    """
    os.chdir(repo_root)

    config = Config()
    config.args_cores_root = [repo_root]
    config.args_no_export = True

    fs = Fusesoc(config)
    if mappings:
        fs.cm.db.mapping_set(mappings)

    core = fs.get_core(core_name)
    if core is None:
        raise RuntimeError(f"core '{core_name}' not found")

    sanitized = core.name.sanitized_name
    work_root = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                             "fusesoc-export-" + sanitized)
    os.makedirs(work_root, exist_ok=True)

    edalizer = Edalizer(
        toplevel=core.name,
        flags={"target": target},
        core_manager=fs.cm,
        work_root=work_root,
        export_root=None,
        system_name=None,
        resolve_env_vars=False,
    )

    try:
        edalizer.run()
    except (DependencyError, RuntimeError) as e:
        raise RuntimeError(str(e)) from e

    return edalizer.edam, work_root, sanitized


def _default_output(repo_root: str, fmt: str, sanitized: str) -> str:
    if fmt == "flist":
        return os.path.join(repo_root, "build", sanitized, "flist",
                            f"{sanitized}.f")
    return os.path.join(repo_root, "Bender.yml")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a .f file or Bender.yml from a FuseSoC core",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version",
                        action="store_true",
                        help="Show version and exit")
    parser.add_argument(
        "core",
        nargs="?",
        help="FuseSoC core VLNV, e.g. lowrisc:systems:top_earlgrey")
    parser.add_argument("--target",
                        default="lint",
                        help="FuseSoC target (default: lint)")
    parser.add_argument(
        "--mapping",
        metavar="VLNV",
        action="append",
        default=[],
        help=("VLNV of a mapping core to apply (repeatable). "
              "Selects technology-specific implementations for virtual cores. "
              "E.g. --mapping lowrisc:systems:top_earlgrey:0.1"),
    )
    parser.add_argument(
        "--format",
        choices=["bender", "flist"],
        default="flist",
        help=
        "Output format: 'bender' writes Bender.yml, 'flist' writes a .f file (default: flist)",
    )
    parser.add_argument(
        "--cores-root",
        default=".",
        help="Root directory to search for .core files (default: .)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=("Output path. Defaults to build/<core>/flist/<core>.f (flist) "
              "or Bender.yml in the repo root (bender)."),
    )
    args = parser.parse_args()

    if args.version:
        show_and_exit(__file__, ["fusesoc", "pyyaml"])

    if not args.core:
        parser.error("the following arguments are required: core")

    repo_root = os.path.abspath(args.cores_root)

    # Resolve explicit --output BEFORE chdir so relative paths are anchored to
    # the caller's CWD, not the repo root (important inside nix FHS sandbox).
    explicit_out = Path(os.path.abspath(args.output)) if args.output else None

    try:
        edam, work_root, sanitized = resolve(args.core, args.mapping,
                                             repo_root, args.target)
    except (RuntimeError, DependencyError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # Default output uses sanitized name (includes version) for consistency
    # with slang_lint.py. repo_root is absolute so chdir above doesn't matter.
    out_path = explicit_out or Path(
        _default_output(repo_root, args.format, sanitized))

    out_path.parent.mkdir(parents=True, exist_ok=True)

    files, include_dirs, defines = _collect(edam, work_root)

    if args.format == "flist":
        out_path.write_text(edam_to_flist(files, include_dirs, defines))
    else:
        with open(out_path, "w") as fh:
            yaml.dump(edam_to_bender(edam, files, include_dirs, defines),
                      fh,
                      default_flow_style=False,
                      sort_keys=False)

    toplevel = edam.get("toplevel", [])
    if isinstance(toplevel, str):
        toplevel = [toplevel] if toplevel else []
    toplevel_str = f", toplevel: {' '.join(toplevel)}" if toplevel else ""
    print(f"Wrote {out_path} ({args.format}): "
          f"{len(files)} source files, "
          f"{len(include_dirs)} include dirs{toplevel_str}")


if __name__ == "__main__":
    main()
